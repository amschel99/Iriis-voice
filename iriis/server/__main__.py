"""python -m iriis.server  (honours $PORT, as Railway sets it)"""
import os

import uvicorn


def main() -> None:
    from iriis.config import load_config
    cfg = load_config()
    port = int(os.environ.get("PORT", cfg.server.port))
    uvicorn.run("iriis.server.app:app", host=cfg.server.host, port=port, proxy_headers=True,
                forwarded_allow_ips="*", ws_ping_interval=20, ws_ping_timeout=20, log_level="info")


if __name__ == "__main__":
    main()
