import uvicorn
from fastapi import FastAPI

app = FastAPI(
    title="MktMtm",
    version="0.1.0",
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def main() -> None:
    uvicorn.run(
        "mktmtm.api:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
    )
