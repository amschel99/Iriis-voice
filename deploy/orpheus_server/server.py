"""Container entrypoint: python -m orpheus_server.server  (listens on $PORT, default 80)."""
import os

import uvicorn

from orpheus_server.core import OrpheusModel, create_app

app = create_app(OrpheusModel(cache_dir=os.environ.get("HF_HOME")))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "80")), log_level="info")
