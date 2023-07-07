"""Generic process mailbox."""

from __future__ import annotations

import socket
import warnings
from collections import defaultdict, deque
from contextlib import contextmanager
from copy import copy
from itertools import count
from time import time

from . import Consumer, Exchange, Producer, Queue
from .clocks import LamportClock
from .common import maybe_declare, oid_from
from .exceptions import InconsistencyError
from .log import get_logger
from .matcher import match
from .utils.functional import maybe_evaluate, reprcall
from .utils.objects import cached_property
from .utils.uuid import uuid

REPLY_QUEUE_EXPIRES = 10

W_PIDBOX_IN_USE = """\
A node named {node.hostname} is already using this process mailbox!

Maybe you forgot to shutdown the other node or did not do so properly?
Or if you meant to start multiple nodes on the same host please make sure
you give each node a unique node name!
"""

__all__ = ('Node', 'Mailbox')
logger = get_logger(__name__)
debug, error = logger.debug, logger.error

# rabbitmq queue types.
RMQ_CLASSIC_QUEUE = 'classic'
RMQ_QUORUM_QUEUE = 'quorum'
RMQ_STREAM_QUEUE = 'stream'


class Node:
    """Mailbox node."""

    #: hostname of the node.
    hostname = None

    #: the :class:`Mailbox` this is a node for.
    mailbox = None

    #: map of method name/handlers.
    handlers = None

    #: current context (passed on to handlers)
    state = None

    #: current channel.
    channel = None

    def __init__(self, hostname, state=None, channel=None,
                 handlers=None, mailbox=None, queue_type=RMQ_CLASSIC_QUEUE):
        self.channel = channel
        self.mailbox = mailbox
        self.hostname = hostname
        self.state = state
        self.adjust_clock = self.mailbox.clock.adjust
        if handlers is None:
            handlers = {}
        self.handlers = handlers
        self.msg_index = 0
        self.queue_type = queue_type

    def Consumer(self, channel=None, no_ack=True, accept=None, **options):
        queue = self.mailbox.get_queue(self.hostname)

        def verify_exclusive(name, messages, consumers):
            if consumers:
                warnings.warn(W_PIDBOX_IN_USE.format(node=self))
        if self.queue_type == RMQ_STREAM_QUEUE:
            no_ack = False
            prefetch_count = 32
        else:
            # Only verify when not using stream queue.
            prefetch_count = None
            queue.on_declared = verify_exclusive

        return Consumer(
            channel or self.channel, [queue], no_ack=no_ack,
            accept=self.mailbox.accept if accept is None else accept,
            prefetch_count=prefetch_count,
            **options
        )

    def handler(self, fun):
        self.handlers[fun.__name__] = fun
        return fun

    def on_decode_error(self, message, exc):
        error('Cannot decode message: %r', exc, exc_info=1)

    def listen(self, channel=None, callback=None):
        def handle_message_by_ack(body, message):
            if callback:
                callback(body, message)
            else:
                self.handle_message(message)
            if self.queue_type == RMQ_STREAM_QUEUE:
                debug(f"pidbox ack message[{self.msg_index}]: {body}")
                message.ack()
            self.msg_index += 1
        consumer = self.Consumer(channel=channel,
                                 no_ack=False,
                                 callbacks=[handle_message_by_ack],
                                 on_decode_error=self.on_decode_error)
        consumer.consume()
        return consumer

    def dispatch(self, method, arguments=None,
                 reply_to=None, ticket=None, **kwargs):
        arguments = arguments or {}
        debug('pidbox received method %s [reply_to:%s ticket:%s]',
              reprcall(method, (), kwargs=arguments), reply_to, ticket)
        handle = reply_to and self.handle_call or self.handle_cast
        try:
            reply = handle(method, arguments)
        except SystemExit:
            raise
        except Exception as exc:
            error('pidbox command error: %r', exc, exc_info=1)
            reply = {'error': repr(exc)}

        if reply_to:
            self.reply({self.hostname: reply},
                       exchange=reply_to['exchange'],
                       routing_key=reply_to['routing_key'],
                       ticket=ticket)
        return reply

    def handle(self, method, arguments=None):
        arguments = {} if not arguments else arguments
        return self.handlers[method](self.state, **arguments)

    def handle_call(self, method, arguments):
        return self.handle(method, arguments)

    def handle_cast(self, method, arguments):
        return self.handle(method, arguments)

    def handle_message(self, body, message=None):
        destination = body.get('destination')
        pattern = body.get('pattern')
        matcher = body.get('matcher')
        if message:
            self.adjust_clock(message.headers.get('clock') or 0)
        hostname = self.hostname
        run_dispatch = False
        if destination:
            if hostname in destination:
                run_dispatch = True
        elif pattern and matcher:
            if match(hostname, pattern, matcher):
                run_dispatch = True
        else:
            run_dispatch = True
        if run_dispatch:
            return self.dispatch(**body)
    dispatch_from_message = handle_message

    def reply(self, data, exchange, routing_key, ticket, **kwargs):
        self.mailbox._publish_reply(data, exchange, routing_key, ticket,
                                    channel=self.channel,
                                    serializer=self.mailbox.serializer)


class Mailbox:
    """Process Mailbox."""

    node_cls = Node
    exchange_fmt = '%s.pidbox'
    reply_exchange_fmt = 'reply.%s.pidbox'

    #: Name of application.
    namespace = None

    #: Connection (if bound).
    connection = None

    #: Exchange type (usually direct, or fanout for broadcast).
    type = 'direct'

    #: mailbox exchange (init by constructor).
    exchange = None

    #: exchange to send replies to.
    reply_exchange = None

    #: Only accepts json messages by default.
    accept = ['json']

    #: Message serializer
    serializer = None

    def __init__(self, namespace,
                 type='direct', connection=None, clock=None,
                 accept=None, serializer=None, producer_pool=None,
                 queue_ttl=None, queue_expires=None,
                 reply_queue_ttl=None, reply_queue_expires=10.0,
                 queue_type=RMQ_CLASSIC_QUEUE):
        self.namespace = namespace
        self.connection = connection
        self.type = type
        self.clock = LamportClock() if clock is None else clock
        self.exchange = self._get_exchange(self.namespace, type)
        self.reply_exchange = self._get_reply_exchange(self.namespace)
        self.unclaimed = defaultdict(deque)
        self.accept = self.accept if accept is None else accept
        self.serializer = self.serializer if serializer is None else serializer
        self.queue_ttl = queue_ttl
        self.queue_expires = queue_expires
        self.reply_queue_ttl = reply_queue_ttl
        self.reply_queue_expires = reply_queue_expires
        self._producer_pool = producer_pool
        self.queue_type = queue_type

    def __call__(self, connection):
        bound = copy(self)
        bound.connection = connection
        return bound

    def Node(self, hostname=None, state=None, channel=None, handlers=None):
        hostname = hostname or socket.gethostname()
        return self.node_cls(hostname, state, channel, handlers, mailbox=self,
                             queue_type=self.queue_type)

    def call(self, destination, command, kwargs=None,
             timeout=None, callback=None, channel=None):
        kwargs = {} if not kwargs else kwargs
        return self._broadcast(command, kwargs, destination,
                               reply=True, timeout=timeout,
                               callback=callback,
                               channel=channel)

    def cast(self, destination, command, kwargs=None):
        kwargs = {} if not kwargs else kwargs
        return self._broadcast(command, kwargs, destination, reply=False)

    def abcast(self, command, kwargs=None):
        kwargs = {} if not kwargs else kwargs
        return self._broadcast(command, kwargs, reply=False)

    def multi_call(self, command, kwargs=None, timeout=1,
                   limit=None, callback=None, channel=None):
        kwargs = {} if not kwargs else kwargs
        return self._broadcast(command, kwargs, reply=True,
                               timeout=timeout, limit=limit,
                               callback=callback,
                               channel=channel)

    def get_reply_queue(self):
        oid = self.oid
        if self.queue_type == RMQ_STREAM_QUEUE:
            # all worker share the same stream queue.
            queue_arguments = {
                "x-queue-type": "stream",
                "max-length-bytes": 1e9,  # 10GB in disk.
            }
            return Queue(
                f'stream.{self.reply_exchange.name}',
                exchange=self.reply_exchange,
                routing_key=oid,
                queue_arguments=queue_arguments
            )
        else:
            return Queue(
                f'{oid}.{self.reply_exchange.name}',
                exchange=self.reply_exchange,
                routing_key=oid,
                durable=False,
                auto_delete=True,
                expires=self.reply_queue_expires,
                message_ttl=self.reply_queue_ttl,
            )

    @cached_property
    def reply_queue(self):
        return self.get_reply_queue()

    def get_queue(self, hostname):
        # Start consuming messages from the queues
        if self.queue_type == RMQ_STREAM_QUEUE:
            # all worker share the same stream queue.
            queue_arguments = {
                "x-queue-type": "stream",
                "max-length-bytes": 1e9,  # 10GB in disk.
            }
            return Queue(
                f'stream.{self.namespace}.pidbox',
                exchange=self.exchange,
                queue_arguments=queue_arguments
            )
        else:
            return Queue(
                f'{hostname}.{self.namespace}.pidbox',
                exchange=self.exchange,
                durable=False,
                auto_delete=True,
                expires=self.queue_expires,
                message_ttl=self.queue_ttl,
            )

    @contextmanager
    def producer_or_acquire(self, producer=None, channel=None):
        if producer:
            yield producer
        elif self.producer_pool:
            with self.producer_pool.acquire() as producer:
                yield producer
        else:
            yield Producer(channel, auto_declare=False)

    def _publish_reply(self, reply, exchange, routing_key, ticket,
                       channel=None, producer=None, **opts):
        chan = channel or self.connection.default_channel
        exchange = Exchange(exchange, exchange_type='direct',
                            delivery_mode='transient',
                            durable=False)
        with self.producer_or_acquire(producer, chan) as producer:
            try:
                producer.publish(
                    reply, exchange=exchange, routing_key=routing_key,
                    declare=[exchange], headers={
                        'ticket': ticket, 'clock': self.clock.forward(),
                    }, retry=True,
                    **opts
                )
            except InconsistencyError:
                # queue probably deleted and no one is expecting a reply.
                pass

    def _publish(self, type, arguments, destination=None,
                 reply_ticket=None, channel=None, timeout=None,
                 serializer=None, producer=None, pattern=None, matcher=None):
        message = {'method': type,
                   'arguments': arguments,
                   'destination': destination,
                   'pattern': pattern,
                   'matcher': matcher}
        chan = channel or self.connection.default_channel
        exchange = self.exchange
        if reply_ticket:
            maybe_declare(self.reply_queue(chan))
            message.update(ticket=reply_ticket,
                           reply_to={'exchange': self.reply_exchange.name,
                                     'routing_key': self.oid})
        serializer = serializer or self.serializer
        with self.producer_or_acquire(producer, chan) as producer:
            producer.publish(
                message, exchange=exchange.name, declare=[exchange],
                headers={'clock': self.clock.forward(),
                         'expires': time() + timeout if timeout else 0},
                serializer=serializer, retry=True,
            )

    def _broadcast(self, command, arguments=None, destination=None,
                   reply=False, timeout=1, limit=None,
                   callback=None, channel=None, serializer=None,
                   pattern=None, matcher=None):
        if destination is not None and \
                not isinstance(destination, (list, tuple)):
            raise ValueError(
                'destination must be a list/tuple not {}'.format(
                    type(destination)))
        if (pattern is not None and not isinstance(pattern, str) and
                matcher is not None and not isinstance(matcher, str)):
            raise ValueError(
                'pattern and matcher must be '
                'strings not {}, {}'.format(type(pattern), type(matcher))
            )

        arguments = arguments or {}
        reply_ticket = reply and uuid() or None
        chan = channel or self.connection.default_channel

        # Set reply limit to number of destinations (if specified)
        if limit is None and destination:
            limit = destination and len(destination) or None

        serializer = serializer or self.serializer
        self._publish(command, arguments, destination=destination,
                      reply_ticket=reply_ticket,
                      channel=chan,
                      timeout=timeout,
                      serializer=serializer,
                      pattern=pattern,
                      matcher=matcher)

        if reply_ticket:
            return self._collect(reply_ticket, limit=limit,
                                 timeout=timeout,
                                 callback=callback,
                                 channel=chan)

    def _collect(self, ticket,
                 limit=None, timeout=1, callback=None,
                 channel=None, accept=None):
        if accept is None:
            accept = self.accept
        chan = channel or self.connection.default_channel
        queue = self.reply_queue
        consumer = Consumer(chan, [queue], accept=accept, no_ack=True)
        responses = []
        unclaimed = self.unclaimed
        adjust_clock = self.clock.adjust

        try:
            return unclaimed.pop(ticket)
        except KeyError:
            pass

        def on_message(body, message):
            # ticket header added in kombu 2.5
            header = message.headers.get
            adjust_clock(header('clock') or 0)
            expires = header('expires')
            if expires and time() > expires:
                return
            this_id = header('ticket', ticket)
            if this_id == ticket:
                if callback:
                    callback(body)
                responses.append(body)
            else:
                unclaimed[this_id].append(body)

        consumer.register_callback(on_message)
        try:
            with consumer:
                for i in limit and range(limit) or count():
                    try:
                        self.connection.drain_events(timeout=timeout)
                    except socket.timeout:
                        break
                return responses
        finally:
            chan.after_reply_message_received(queue.name)

    def _get_exchange(self, namespace, type):
        exchange = Exchange(self.exchange_fmt % namespace,
                            type=type,
                            durable=False,
                            delivery_mode='transient')
        error(f"Exchange: {exchange}, {type}")
        return exchange

    def _get_reply_exchange(self, namespace):
        return Exchange(self.reply_exchange_fmt % namespace,
                        type='direct',
                        durable=False,
                        delivery_mode='transient')

    @property
    def oid(self):
        return oid_from(self)

    @cached_property
    def producer_pool(self):
        return maybe_evaluate(self._producer_pool)
