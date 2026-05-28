from __future__ import annotations

import logging
import time
from typing import Callable, Protocol

from .models import AppConfig, SiteConfig
from .points import AI_POINTS, AO_POINTS, enabled_ai_points

LOGGER = logging.getLogger(__name__)

AoCommandCallback = Callable[[str, int, float], bool]


class Dnp3Gateway(Protocol):
    available: bool

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def send_measurements(self, site_id: str, values: dict[int, int], periodic: bool) -> None: ...

    def set_command_callback(self, callback: AoCommandCallback) -> None: ...


class NullDnp3Gateway:
    available = False

    def __init__(self) -> None:
        self.command_callback: AoCommandCallback | None = None

    def start(self) -> None:
        LOGGER.warning("DNP3 backend is not active; measurements will be buffered only")

    def stop(self) -> None:
        return

    def send_measurements(self, site_id: str, values: dict[int, int], periodic: bool) -> None:
        raise RuntimeError("DNP3 backend is not available")

    def set_command_callback(self, callback: AoCommandCallback) -> None:
        self.command_callback = callback


def create_dnp3_gateway(config: AppConfig) -> Dnp3Gateway:
    if config.runtime.dnp3_backend == "null":
        return NullDnp3Gateway()
    try:
        from pydnp3 import asiopal, asiodnp3, opendnp3, openpal  # type: ignore
    except Exception as exc:
        if config.runtime.dnp3_backend == "pydnp3":
            raise RuntimeError("pydnp3 backend requested but import failed") from exc
        LOGGER.warning("pydnp3 import failed; using NullDnp3Gateway: %s", exc)
        return NullDnp3Gateway()
    return Pydnp3Gateway(config, asiopal, asiodnp3, opendnp3, openpal)


class Pydnp3Gateway:
    def __init__(self, config: AppConfig, asiopal, asiodnp3, opendnp3, openpal):
        self.config = config
        self.asiopal = asiopal
        self.asiodnp3 = asiodnp3
        self.opendnp3 = opendnp3
        self.openpal = openpal
        self.manager = None
        self.channel = None
        self.channel_listener = None
        self.outstations: dict[str, object] = {}
        self.outstation_apps: dict[str, object] = {}
        self.command_handlers: dict[str, object] = {}
        self.command_callback: AoCommandCallback | None = None
        self.available = False

    def set_command_callback(self, callback: AoCommandCallback) -> None:
        self.command_callback = callback

    def start(self) -> None:
        log_levels = self.opendnp3.levels.NORMAL | self.opendnp3.levels.ALL_COMMS
        self.manager = self.asiodnp3.DNP3Manager(1, self.asiodnp3.ConsoleLogger().Create())
        retry = self.asiopal.ChannelRetry().Default()
        self.channel_listener = _make_channel_listener(self.asiodnp3)
        self.channel = self.manager.AddTCPServer(
            "dreams-outstation-server",
            log_levels,
            retry,
            self.config.dnp3.bind,
            self.config.dnp3.port,
            self.channel_listener,
        )

        for site in self.config.enabled_sites():
            stack_config = self._build_stack_config(site)
            app = _make_outstation_application(self.opendnp3)
            handler = _make_command_handler(self.opendnp3, site.key, self._handle_ao_command)
            outstation = self.channel.AddOutstation(f"site-{site.key}", handler, app, stack_config)
            outstation.Enable()
            self.outstation_apps[site.key] = app
            self.command_handlers[site.key] = handler
            self.outstations[site.key] = outstation
            LOGGER.info("DNP3 outstation enabled logger=%s address=%s", site.key, site.dnp3_address)

        self.available = True
        LOGGER.info("DNP3 TCP server listening on %s:%s", self.config.dnp3.bind, self.config.dnp3.port)

    def stop(self) -> None:
        self.available = False
        try:
            if self.manager is not None:
                self.manager.Shutdown()
        finally:
            self.manager = None
            self.channel = None
            self.channel_listener = None
            self.outstations.clear()
            self.outstation_apps.clear()
            self.command_handlers.clear()

    def send_measurements(self, site_id: str, values: dict[int, int], periodic: bool) -> None:
        outstation = self.outstations.get(site_id)
        if outstation is None:
            raise KeyError(f"No DNP3 outstation for logger {site_id}")

        flags_value = 0x81 if periodic else 0x01
        flags = self.opendnp3.Flags(flags_value)
        timestamp = self.opendnp3.DNPTime(int(time.time() * 1000))
        mode = self.opendnp3.EventMode.Force
        builder = self.asiodnp3.UpdateBuilder()
        for index, value in sorted(values.items()):
            if index not in AI_POINTS:
                continue
            measurement = self.opendnp3.Analog(float(value), flags, timestamp)
            builder.Update(measurement, int(index), mode)
        outstation.Apply(builder.Build())
        LOGGER.info("Applied DNP3 measurements logger=%s count=%s periodic=%s", site_id, len(values), periodic)

    def _build_stack_config(self, site: SiteConfig):
        max_index = max(enabled_ai_points(self.config.dnp3.include_spare_point_31).keys())
        stack_config = self.asiodnp3.OutstationStackConfig(self.opendnp3.DatabaseSizes.AllTypes(max_index + 1))
        stack_config.outstation.eventBufferConfig = self.opendnp3.EventBufferConfig().AllTypes(
            self.config.dnp3.site_buffer_limit
        )
        stack_config.outstation.params.allowUnsolicited = True
        stack_config.outstation.params.unsolClassMask = self.opendnp3.ClassField.AllEventClasses()
        stack_config.outstation.params.unsolConfirmTimeout = self.openpal.TimeDuration.Seconds(
            self.config.dnp3.application_confirm_timeout_seconds
        )
        stack_config.outstation.params.solConfirmTimeout = self.openpal.TimeDuration.Seconds(
            self.config.dnp3.application_confirm_timeout_seconds
        )
        stack_config.outstation.params.maxTxFragSize = self.config.dnp3.application_fragment_size
        stack_config.outstation.params.maxRxFragSize = self.config.dnp3.application_fragment_size
        stack_config.link.LocalAddr = site.dnp3_address
        stack_config.link.RemoteAddr = self.config.dnp3.master_address
        stack_config.link.KeepAliveTimeout = self.openpal.TimeDuration().Max()
        self._configure_database(stack_config.dbConfig)
        return stack_config

    def _configure_database(self, db_config) -> None:
        clazz = (
            self.opendnp3.PointClass.Class2
            if self.config.dnp3.ai_event_class == "class2"
            else self.opendnp3.PointClass.Class1
        )
        for index, point in enabled_ai_points(self.config.dnp3.include_spare_point_31).items():
            db_config.analog[index].clazz = clazz
            db_config.analog[index].svariation = self._static_variation(point.static_variation)
            if point.event_variation == 6:
                db_config.analog[index].evariation = self.opendnp3.EventAnalogVariation.Group32Var6
            else:
                db_config.analog[index].evariation = self.opendnp3.EventAnalogVariation.Group32Var3

    def _static_variation(self, variation: int):
        if variation == 6:
            return self.opendnp3.StaticAnalogVariation.Group30Var6
        return self.opendnp3.StaticAnalogVariation.Group30Var3

    def _handle_ao_command(self, site_id: str, ao_index: int, value: float) -> bool:
        if self.command_callback is None:
            return False
        return self.command_callback(site_id, ao_index, value)


def _make_outstation_application(opendnp3):
    class Impl(opendnp3.IOutstationApplication):
        def __init__(self):
            super().__init__()

        def ColdRestartSupport(self):
            return opendnp3.RestartMode.UNSUPPORTED

        def WarmRestartSupport(self):
            return opendnp3.RestartMode.UNSUPPORTED

        def SupportsAssignClass(self):
            return False

        def SupportsWriteAbsoluteTime(self):
            return False

        def SupportsWriteTimeAndInterval(self):
            return False

        def GetApplicationIIN(self):
            return opendnp3.ApplicationIIN()

    return Impl()


def _make_command_handler(opendnp3, site_id: str, callback: AoCommandCallback):
    class Impl(opendnp3.ICommandHandler):
        def __init__(self):
            super().__init__()

        def Start(self):
            return None

        def End(self):
            return None

        def Select(self, command, index):
            if int(index) not in AO_POINTS:
                return opendnp3.CommandStatus.NOT_SUPPORTED
            return opendnp3.CommandStatus.SUCCESS

        def Operate(self, command, index, op_type):
            ao_index = int(index)
            if ao_index not in AO_POINTS:
                return opendnp3.CommandStatus.NOT_SUPPORTED
            raw_value = float(getattr(command, "value", 0))
            ok = callback(site_id, ao_index, raw_value)
            return opendnp3.CommandStatus.SUCCESS if ok else opendnp3.CommandStatus.HARDWARE_ERROR

    return Impl()


def _make_channel_listener(asiodnp3):
    class Impl(asiodnp3.IChannelListener):
        def __init__(self):
            super().__init__()

        def OnStateChange(self, state):
            LOGGER.info("DNP3 channel state changed: %s", state)

    return Impl()
