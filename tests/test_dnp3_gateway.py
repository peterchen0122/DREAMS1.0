import unittest
from types import SimpleNamespace

from dreams_outstation.config import load_config
from dreams_outstation.dnp3_gateway import Pydnp3Gateway, _make_outstation_application


class _FakeTimeDuration:
    @staticmethod
    def Seconds(value):
        return ("seconds", value)

    def Max(self):
        return ("max",)


class _FakeOpenPal:
    TimeDuration = _FakeTimeDuration


class _FakeOutstationStackConfig:
    def __init__(self, size):
        self.outstation = SimpleNamespace(params=SimpleNamespace(), eventBufferConfig=None)
        self.link = SimpleNamespace()
        self.dbConfig = SimpleNamespace(analog={index: SimpleNamespace() for index in range(size)})


class _FakeAsiodnp3:
    OutstationStackConfig = _FakeOutstationStackConfig


class _FakeOpenDnp3:
    class IOutstationApplication:
        def __init__(self):
            pass

    class RestartMode:
        SUPPORTED_DELAY_COARSE = "supported-delay-coarse"

    class ApplicationIIN:
        pass

    class DatabaseSizes:
        @staticmethod
        def AllTypes(size):
            return size

    class EventBufferConfig:
        def AllTypes(self, size):
            return ("all-types", size)

    class ClassField:
        @staticmethod
        def AllEventClasses():
            return "all-event-classes"

    class PointClass:
        Class1 = "class1"
        Class2 = "class2"

    class StaticAnalogVariation:
        Group30Var3 = "group30-var3"
        Group30Var6 = "group30-var6"

    class EventAnalogVariation:
        Group32Var3 = "group32-var3"
        Group32Var6 = "group32-var6"


class Dnp3GatewayTests(unittest.TestCase):
    def test_restart_support_and_iin_are_enabled(self):
        app = _make_outstation_application(_FakeOpenDnp3)
        outstation = SimpleNamespace(called=False)

        def set_restart_iin():
            outstation.called = True

        outstation.SetRestartIIN = set_restart_iin
        gateway = Pydnp3Gateway(None, None, None, None, None)
        gateway._set_restart_iin(outstation, "*")

        self.assertEqual(app.ColdRestartSupport(), _FakeOpenDnp3.RestartMode.SUPPORTED_DELAY_COARSE)
        self.assertEqual(app.WarmRestartSupport(), _FakeOpenDnp3.RestartMode.SUPPORTED_DELAY_COARSE)
        self.assertTrue(outstation.called)

    def test_stack_config_sets_class2_and_unsolicited_retry_timeout(self):
        config = load_config("config/config.yaml")
        gateway = Pydnp3Gateway(config, None, _FakeAsiodnp3, _FakeOpenDnp3, _FakeOpenPal)

        stack_config = gateway._build_stack_config(config.enabled_sites()[0])

        self.assertEqual(stack_config.dbConfig.analog[7].clazz, _FakeOpenDnp3.PointClass.Class2)
        self.assertEqual(
            stack_config.outstation.params.unsolRetryTimeout,
            ("seconds", config.dnp3.application_confirm_timeout_seconds),
        )


if __name__ == "__main__":
    unittest.main()
