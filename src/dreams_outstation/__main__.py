from __future__ import annotations

import argparse
import os
from pathlib import Path

from .config import load_config
from .logging_config import configure_logging
from .service import DreamsOutstationService


def main() -> None:
    parser = argparse.ArgumentParser(description="DREAMS DNP3 Outstation MQTT bridge")
    parser.add_argument("--config", default="config/config.yaml", help="Path to YAML config file")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    config = load_config(args.config)
    configure_logging(config.runtime.log_path, verbose=args.verbose)
    pid_path = Path(config.runtime.log_path).with_suffix(".pid")
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    service = DreamsOutstationService(config)
    try:
        service.run_forever()
    finally:
        try:
            if pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pid_path.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()
