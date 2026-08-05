from pathlib import Path
import subprocess
import unittest


MAIL_ROOT = Path(__file__).resolve().parents[1]


class ServiceWorkerBehaviorTests(unittest.TestCase):
    def test_offline_shell_and_network_only_routes(self):
        subprocess.run(
            [
                "node",
                str(Path(__file__).with_name("service_worker_behavior.js")),
                str(MAIL_ROOT / "static" / "sw.js"),
            ],
            check=True,
        )


if __name__ == "__main__":
    unittest.main()
