"""Print the websocket message limit for the default START_COMMAND: `python -m app.scripts.chat_frame_limit`."""

from app.config import get_settings


def main() -> None:
    print(get_settings().chat_frame_max_bytes())


if __name__ == "__main__":
    main()
