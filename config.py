import os
from dataclasses import dataclass


@dataclass
class Config:
    database_url: str
    bb_url: str
    bb_password: str
    openai_api_key: str
    public_url: str  # public URL of this server so BB can reach the webhook

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            database_url=os.environ["DATABASE_URL"],
            bb_url=os.environ.get("BB_URL", "http://localhost:1234"),
            bb_password=os.environ.get("BB_PASSWORD", ""),
            openai_api_key=os.environ["OPENAI_API_KEY"],
            public_url=os.environ.get("PUBLIC_URL", "http://localhost:8001"),
        )


config = Config.from_env()