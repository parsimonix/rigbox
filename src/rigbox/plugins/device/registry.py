"""Register device transports and message protocols independently of command dispatch.

Plugins share a DeviceRegistry through Registry.extension(DeviceRegistry), even when their registration
hooks run before the device command hook. Each command registry owns its own set of device factories.
"""

from __future__ import annotations

from rigbox.core.registry import CommandError
from rigbox.plugins.device.protocol import ProtocolFactory, ProtocolSession
from rigbox.plugins.device.transport import (
    Transport,
    TransportFactory,
    TransportOptions,
    scheme_of,
)


class DeviceRegistry:
    """Keep transport and message protocol factories for the device subsystem."""

    def __init__(self) -> None:
        """Create an empty device registry without opening resources or registering defaults."""
        self.transports: dict[str, TransportFactory] = {}
        self.protocols: dict[str, ProtocolFactory] = {}

    def add_transport(self, scheme: str, factory: TransportFactory) -> None:
        """Register a factory for URLs of the form <scheme>://....

        Args:
            scheme: Case-insensitive URL scheme.
            factory: Factory that opens the device communication channel.

        Raises:
            ValueError: If the scheme is already registered.
        """
        scheme = scheme.lower()
        if scheme in self.transports:
            raise ValueError(f"duplicate transport scheme: {scheme}")
        self.transports[scheme] = factory

    def open_transport(self, url: str, options: TransportOptions) -> Transport:
        """Open a channel using the factory registered for its URL scheme.

        Args:
            url: Device address, defaulting to serial if no scheme is given.
            options: Transport-specific I/O settings.

        Returns:
            The opened transport, whose lifecycle belongs to the caller.

        Raises:
            CommandError: If no transport handles the URL or opening fails.
        """
        scheme = scheme_of(url)
        factory = self.transports.get(scheme)
        if factory is None:
            known = ", ".join(sorted(self.transports)) or "none"
            raise CommandError(
                f"no transport for '{scheme}' URLs (known schemes: {known})"
            )
        try:
            return factory(url, options)
        except CommandError:
            raise
        except Exception as error:  # noqa: BLE001 - Do not catch blind exception
            # factories are plugin code; never let one crash the host
            raise CommandError(f"cannot open {url}: {error}") from None

    def add_protocol(self, name: str, factory: ProtocolFactory) -> None:
        """Register a message session factory; raw is reserved for unframed I/O.

        Args:
            name: Case-insensitive identifier used by --protocol.
            factory: Session factory receiving the connection's transport scheme.

        Raises:
            ValueError: If the identifier is empty, reserved or already registered.
        """
        name = name.strip().lower()
        if not name or name == "raw" or name in self.protocols:
            raise ValueError(
                f"duplicate, reserved or empty protocol identifier: {name!r}"
            )
        self.protocols[name] = factory

    def create_protocol(self, name: str, scheme: str) -> ProtocolSession:
        """Create a message session without opening or owning the transport.

        Args:
            name: Registered protocol identifier.
            scheme: Transport scheme used to validate compatibility.

        Returns:
            A fresh stateful session.

        Raises:
            CommandError: If the protocol is unknown, incompatible or cannot be created.
        """
        name = name.strip().lower()
        factory = self.protocols.get(name)
        if factory is None:
            known = ", ".join(["raw", *sorted(self.protocols)])
            raise CommandError(f"unknown protocol '{name}' (known protocols: {known})")
        try:
            return factory(scheme)
        except CommandError:
            raise
        except (
            Exception
        ) as error:  # factories are plugin code; never let one crash the host
            raise CommandError(f"cannot create protocol '{name}': {error}") from error
