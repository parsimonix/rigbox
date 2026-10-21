"""Continuously parse one connection's input and route responses separately from bounded unsolicited traffic.

Only this worker reads the transport. Protocol parsing and request preparation share a condition lock;
transport writes run outside that lock so receiving can continue during a bounded write.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from threading import Condition, Event, Thread
from time import monotonic
from typing import ClassVar

from rigbox.core.registry import CommandError
from rigbox.plugins.device.protocol import (
    ContinuousProtocol,
    Message,
    ProtocolDiagnostics,
    ProtocolSession,
    RequestPlan,
)
from rigbox.plugins.device.transport import Transport


class _Input:
    def __init__(self, chunk: bytes, ended: bool) -> None:
        self.chunk = chunk
        self.ended = ended

    def read_chunk(self, timeout_s: float) -> bytes:
        chunk, self.chunk = self.chunk, b""
        if not chunk and self.ended:
            raise EOFError("end of stream")
        return chunk

    def write(self, data: bytes) -> None:
        raise RuntimeError("receive parsing must not write to the channel")


class MessageReceiver:
    """Own continuous reads and a bounded drop-oldest queue for one protocol session."""

    MAX_PACKETS: ClassVar[int] = 256
    MAX_BYTES: ClassVar[int] = 256 * 1024
    POLL_S: ClassVar[float] = 0.05

    def __init__(self, transport: Transport, session: ProtocolSession, protocol: ContinuousProtocol) -> None:
        """Create a receiver without starting its worker.

        Args:
            transport: Connection-owned byte stream.
            session: Stateful parser used exclusively under the condition lock.
            protocol: Request planning capability of that same session.
        """
        self._transport = transport
        self._session = session
        self._protocol = protocol
        self._condition = Condition()
        self._stop = Event()
        self._thread: Thread | None = None
        self._queue: deque[Message] = deque()
        self._bytes = 0
        self._plan: RequestPlan | None = None
        self._response: Message | None = None
        self._unresolved = False
        self._error: Exception | None = None
        self._ended = False
        self._dropped = 0
        self._dropped_bytes = 0
        self._reported_dropped = 0
        self._reported_bytes = 0
        self._filtered = 0
        self._filtered_bytes = 0
        self._diagnostics: deque[str] = deque(maxlen=64)
        self._lost_diagnostics = 0

    def start(self) -> None:
        """Start the sole transport reader."""
        self._stop.clear()
        self._thread = Thread(target=self._run, name="rigbox-message-reader", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop and join the reader before closing or switching the transport.

        Raises:
            CommandError: If the transport does not honor its bounded read deadline.
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            if self._thread.is_alive():
                raise CommandError("message reader did not stop; transport must honor read deadlines")

    @property
    def pending_input(self) -> bool:
        """Report whether switching would abandon traffic or ambiguous state.

        Returns:
            Whether buffered, queued, failed or unresolved state remains.
        """
        with self._condition:
            return bool(self._queue) or self._session.pending_input or self._unresolved or self._error is not None

    def stats(self) -> dict[str, int]:
        """Report bounded queue occupancy and cumulative overflow losses.

        Returns:
            Packet/byte queue sizes and overflow loss counters.
        """
        with self._condition:
            return {
                "queued_packets": len(self._queue),
                "queued_wire_bytes": self._bytes,
                "dropped_packets": self._dropped,
                "dropped_wire_bytes": self._dropped_bytes,
            }

    def encode(self, payload: bytes, options: Mapping[str, str] | None) -> bytes:
        """Frame a send without competing with the parser.

        Args:
            payload: Application bytes.
            options: Per-message fields.

        Returns:
            Encoded wire bytes.

        Raises:
            CommandError: If the session failed or a request remains unresolved.
        """
        with self._condition:
            self._check_sendable()
            return self._session.encode(payload, options=options) if options else self._session.encode(payload)

    def receive(self, timeout_s: float) -> Message | None:
        """Wait for the oldest queued message while the worker continues reading.

        Args:
            timeout_s: Overall wait budget; zero polls.

        Returns:
            A message or None on timeout.

        Raises:
            CommandError: If reception failed.
            EOFError: If the stream ended and the queue is empty.
        """
        deadline = monotonic() + timeout_s
        with self._condition:
            while True:
                if self._queue:
                    message = self._queue.popleft()
                    self._bytes -= len(message.wire_data)
                    return message
                self._check_readable()
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)

    def request(self, payload: bytes, timeout_s: float, options: Mapping[str, str] | None) -> Message | None:
        """Arm response routing, write once and await a response outside the unsolicited queue.

        Args:
            payload: Application request.
            timeout_s: Response budget starting after the write.
            options: Request metadata and request-local filtering rules.

        Returns:
            A matching response or None on timeout.

        Raises:
            CommandError: If validation, reception or a previous unresolved request prevents sending.
            OSError: If writing fails.
            EOFError: If the stream ends before a response.
        """
        with self._condition:
            self._check_sendable()
            plan = self._protocol.prepare_request(payload, options=options)
            old = self._queue
            self._queue = deque()
            self._bytes = 0
            for message in old:
                if plan.discard(message):
                    self._filter(message)
                else:
                    self._enqueue(message)
            self._response = None
            self._plan = plan
            self._unresolved = True
        try:
            self._transport.write(plan.wire_data)
            deadline = monotonic() + timeout_s
            with self._condition:
                while self._response is None:
                    self._check_readable()
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        return None
                    self._condition.wait(remaining)
                self._unresolved = False
                return self._response
        finally:
            with self._condition:
                self._plan = None
                self._response = None

    def drain_diagnostics(self) -> list[str]:
        """Return aggregated diagnostics on the caller's thread.

        Returns:
            Parser warnings, explicit queue loss counts and request-filter counts.
        """
        with self._condition:
            messages = list(self._diagnostics)
            self._diagnostics.clear()
            if self._lost_diagnostics:
                messages.append(f"{self._lost_diagnostics} older parser diagnostics omitted")
                self._lost_diagnostics = 0
            count = self._dropped - self._reported_dropped
            size = self._dropped_bytes - self._reported_bytes
            if count:
                messages.append(f"unsolicited queue overflow dropped {count} oldest packets ({size} wire bytes)")
            self._reported_dropped, self._reported_bytes = self._dropped, self._dropped_bytes
            if self._filtered:
                messages.append(f"request filters discarded {self._filtered} packets ({self._filtered_bytes} wire bytes)")
            self._filtered = self._filtered_bytes = 0
            return messages

    def _check_readable(self) -> None:
        if self._error is not None:
            raise CommandError(f"continuous reception failed: {self._error}; close and reconnect") from self._error
        if self._ended:
            raise EOFError("end of stream")

    def _check_sendable(self) -> None:
        self._check_readable()
        if self._unresolved:
            raise CommandError("request is unresolved; close and reconnect before sending again")

    def _filter(self, message: Message) -> None:
        self._filtered += 1
        self._filtered_bytes += len(message.wire_data)

    def _enqueue(self, message: Message) -> None:
        size = len(message.wire_data)
        while self._queue and (len(self._queue) >= self.MAX_PACKETS or self._bytes + size > self.MAX_BYTES):
            old = self._queue.popleft()
            self._bytes -= len(old.wire_data)
            self._dropped += 1
            self._dropped_bytes += len(old.wire_data)
        if size > self.MAX_BYTES:
            self._dropped += 1
            self._dropped_bytes += size
            return
        self._queue.append(message)
        self._bytes += size

    def _route(self, message: Message) -> None:
        if self._plan is not None and self._response is None:
            if self._plan.matches(message):
                self._response = message
                return
            if self._plan.discard(message):
                self._filter(message)
                return
        self._enqueue(message)

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                ended = False
                try:
                    chunk = self._transport.read_chunk(self.POLL_S)
                except EOFError:
                    chunk, ended = b"", True
                channel = _Input(chunk, ended)
                with self._condition:
                    try:
                        while (message := self._session.receive(channel, 0)) is not None:
                            self._route(message)
                    finally:
                        if isinstance(self._session, ProtocolDiagnostics):
                            for text in self._session.drain_diagnostics():
                                if len(self._diagnostics) == self._diagnostics.maxlen:
                                    self._lost_diagnostics += 1
                                self._diagnostics.append(text)
                        self._condition.notify_all()
                if not chunk:
                    self._stop.wait(self.POLL_S)
        except EOFError:
            with self._condition:
                self._ended = True
                self._condition.notify_all()
        except Exception as error:
            # A worker failure must reach the foreground caller, never disappear in a thread traceback.
            with self._condition:
                self._error = error
                if len(self._diagnostics) == self._diagnostics.maxlen:
                    self._lost_diagnostics += 1
                self._diagnostics.append(f"continuous reception failed: {error}; close and reconnect")
                self._condition.notify_all()
