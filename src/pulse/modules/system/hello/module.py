from __future__ import annotations

from fastapi import APIRouter

from ....core.module import BaseModule


class HelloModule(BaseModule):
    name = "hello"
    description = "M0 example module"
    route_prefix = "/api/modules/system/hello"
    tags = ["hello"]

    def register_routes(self, router: APIRouter) -> None:
        @router.get("/ping")
        async def ping() -> dict[str, str]:
            return {
                "module": self.name,
                "message": "pong",
                "brand": "Pulse",
            }

    def handle_intent(
        self,
        intent: str,
        text: str,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        return {
            "module": self.name,
            "intent": intent,
            "echo": text,
            "message": "pong",
            "brand": "Pulse",
            "metadata": dict(metadata or {}),
        }


module = HelloModule()
