from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from pinflow_api.routes import (
    agent,
    auth,
    chips,
    cloud,
    datasheet,
    generate,
    health,
    kicad,
    schematic,
)

app = FastAPI(title="Pinflow API", version="0.1.0")

# Tauri webview origin is `tauri://localhost` (or `https://tauri.localhost` on win/linux);
# in Vite dev the origin is `http://localhost:5173`. Allow both.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "tauri://localhost",
        "https://tauri.localhost",
        "http://localhost:5173",
        "http://localhost:1420",
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(chips.router)
app.include_router(kicad.router)
app.include_router(schematic.router)
app.include_router(datasheet.router)
app.include_router(generate.router)
app.include_router(agent.router)
app.include_router(auth.router)
app.include_router(auth.root_router)
app.include_router(cloud.router)


def main() -> None:
    import uvicorn

    uvicorn.run("pinflow_api.main:app", host="127.0.0.1", port=8787, reload=True)


if __name__ == "__main__":
    main()
