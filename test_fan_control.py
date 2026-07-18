import importlib.util
import os
import pathlib
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


daemon = load("fan_daemon", "fan-daemon.py")
gui = load("fan_gui", "fan-gui.py")


class FanLogicTests(unittest.TestCase):
    def test_interpolation_and_bounds(self):
        curve = daemon.normalize_curve([[80, 140], [50, 0], [95, 250]])
        self.assertEqual(curve, [(50.0, 0), (80.0, 140), (95.0, 198)])
        self.assertEqual(daemon.target_duty(65, curve), 70)
        self.assertEqual(daemon.target_duty(200, curve), 198)

    def test_critical_cooling_bypasses_noise_cap(self):
        curve = [(0, 0), (110, 100)]
        self.assertLess(daemon.target_duty(50, curve, max_duty=60, critical_temp=95), 60)
        self.assertEqual(daemon.target_duty(95, curve, max_duty=60, critical_temp=95), 198)

    def test_curve_validation_deduplicates_temperatures(self):
        self.assertEqual(gui.normalize_curve([[50, 10], [50, 20], [70, 300]]), [(50, 20), (70, 198)])
        with self.assertRaises(ValueError):
            gui.normalize_curve([[50, 10]])

    def test_dashboard_contract(self):
        self.assertIn('id="chart"', gui.HTML)
        self.assertIn('id="curveRows"', gui.HTML)
        self.assertEqual(gui.HTML.count('id="fan1"'), 1)
        self.assertEqual(gui.HTML.count('id="fan2"'), 1)

    def test_nvidia_smi_sensor_parsing(self):
        sensors = gui.parse_nvidia_smi("0, 67, NVIDIA GeForce RTX 4080\n1, 54, NVIDIA RTX A2000\n")
        self.assertEqual([sensor["temp"] for sensor in sensors], [67.0, 54.0])
        self.assertIn("RTX 4080", sensors[0]["label"])

    def test_device_path_override(self):
        with mock.patch.dict(os.environ, {"FAN_CONTROL_DEVICE": "/dev/custom_fan_io"}):
            self.assertEqual(daemon.find_ec_device(), "/dev/custom_fan_io")
            self.assertEqual(gui.find_ec_device(), "/dev/custom_fan_io")


if __name__ == "__main__":
    unittest.main()
