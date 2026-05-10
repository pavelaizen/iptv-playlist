from __future__ import annotations

from app.admin_bootstrap import build_admin_context
from app.admin_web import serve


def main() -> None:
    context = build_admin_context()
    serve(
        bind_host=context.settings.bind_host,
        bind_port=context.settings.bind_port,
        store=context.store,
        service=context.service,
    )


if __name__ == "__main__":
    main()
