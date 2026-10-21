"""Register device transports, message protocols and HID profiles independently of command dispatch.

Plugins share a DeviceRegistry through Registry.extension(DeviceRegistry), even when their registration
hooks run before the device command hook. Each command registry owns its own set of device factories.
"""

from __future__ import annotations

from rigbox.core.registry import CommandError
from rigbox.plugins.device.hid_catalog import HIDDeviceSpec
from rigbox.plugins.device.protocol import ProtocolFactory, ProtocolSession
from rigbox.plugins.device.transport import (
    Transport,
    TransportFactory,
    TransportOptions,
    scheme_of,
)


class DeviceRegistry:
    """Keep transport/protocol factories and known HID profiles for the device subsystem."""

    def __init__(self) -> None:
        """Create an empty device registry without opening resources or registering defaults."""
        self.transports: dict[str, TransportFactory] = {}
        self.protocols: dict[str, ProtocolFactory] = {}
        self.hid_devices: dict[str, HIDDeviceSpec] = {}

    def add_hid_device(self, name: str, spec: HIDDeviceSpec) -> None:
        """Register a named HID profile without enumerating or opening hardware.

        Args:
            name: Case-insensitive identifier, preferably namespaced by the contributing plugin.
            spec: Validated device selectors and report definitions.

        Raises:
            ValueError: If the name is empty, contains whitespace or is already registered.
        """
        name = name.strip().lower()
        if not name or any(character.isspace() for character in name) or name in self.hid_devices:
            raise ValueError(f"duplicate or invalid HID device identifier: {name!r}")
        self.hid_devices[name] = spec

    def add_transport(self, scheme: str, factory: TransportFactory) -> None:
        """Register a factory for URLs of the form <scheme>://....

        Args:
            scheme (str): Case-insensitive URL scheme.
            factory (TransportFactory): Transport factory.

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
            url (str): Device address, defaulting to serial if no scheme is given.
            options (TransportOptions): Transport-specific I/O settings.

        Raises:
            CommandError: If no transport handles the URL or opening fails.

        Returns:
            Transport: The opened transport, whose lifecycle belongs to the caller.
        """
        scheme = scheme_of(url)
        factory = self.transports.get(scheme)
        if factory is None:
            known = ", ".join(sorted(self.transports)) or "none"
            raise CommandError(f"no transport for '{scheme}' URLs (known schemes: {known})")
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
            name (str): Case-insensitive identifier used by --protocol.
            factory (ProtocolFactory): Session factory receiving the connection's transport scheme.

        Raises:
            ValueError: If the identifier is empty, reserved or already registered.
        """
        name = name.strip().lower()
        if not name or name == "raw" or name in self.protocols:
            raise ValueError(f"duplicate, reserved or empty protocol identifier: {name!r}")
        self.protocols[name] = factory

    def create_protocol(self, name: str, scheme: str) -> ProtocolSession:
        """Create a message session without opening or owning the transport.

        Args:
            name (str): Registered protocol identifier.
            scheme (str): Transport scheme used to validate compatibility.

        Raises:
            CommandError: If the protocol is unknown, incompatible or cannot be created.

        Returns:
            ProtocolSession: A fresh stateful session.
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
        except Exception as error:  # factories are plugin code; never let one crash the host
            raise CommandError(f"cannot create protocol '{name}': {error}") from error
