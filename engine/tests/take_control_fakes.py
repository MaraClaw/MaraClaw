import json

from redis.exceptions import ConnectionError as RedisConnectionError

from app.services import take_control_state as state


class SharedRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expires: dict[str, int] = {}
        self.now: int = 1000
        self.available: bool = True

    def advance(self, seconds: int) -> None:
        self.now += seconds
        for key, expiry in list(self.expires.items()):
            if expiry <= self.now:
                self.values.pop(key, None)
                self.expires.pop(key)

    def put(self, key: str, value: str, ttl: int) -> None:
        self.values[key] = value
        self.expires[key] = self.now + ttl

    async def eval(self, script: str, numkeys: int, *parts: str | int) -> object:
        if not self.available:
            raise RedisConnectionError("offline")
        keys = [str(value) for value in parts[:numkeys]]
        args = parts[numkeys:]
        key = keys[0]
        current = self.values.get(key)
        if script == state._GET_REMOTE:
            if current is None:
                return None
            remote = state.RemoteSession.model_validate_json(current).model_copy(update={"last_used": self.now})
            encoded = remote.model_dump_json()
            self.put(key, encoded, int(args[0]))
            return encoded
        if script == state._REGISTER:
            if current is not None and current != "":
                return current
            remote = state.RemoteSession.model_validate_json(str(args[0])).model_copy(update={"last_used": self.now})
            encoded = remote.model_dump_json()
            self.put(key, encoded, int(args[1]))
            return encoded
        if script == state._ACQUIRE:
            if current is not None and current != "":
                return current
            remote_value = self.values.get(keys[1])
            if remote_value is None:
                return ""
            remote = state.RemoteSession.model_validate_json(remote_value)
            lease = state.Lease.model_validate_json(str(args[0])).model_copy(update={"remote_id": remote.remote_id})
            encoded = lease.model_dump_json()
            self.put(key, encoded, int(args[1]))
            self.expires[keys[1]] = self.now + int(args[2])
            return encoded
        if script == state._RENEW:
            if current is None:
                return ""
            lease = state.Lease.model_validate_json(current)
            if (lease.user_id, lease.owner_token) != (args[0], args[1]):
                return ""
            registry = f"{key}:session:{lease.image_type}"
            remote_value = self.values.get(registry)
            if (
                remote_value is None
                or remote_value == ""
                or state.RemoteSession.model_validate_json(remote_value).remote_id != lease.remote_id
            ):
                return ""
            self.expires[key] = self.now + int(args[2])
            self.expires[registry] = self.now + int(args[3])
            return current
        if script == state._RELEASE:
            if current is None:
                return 0
            lease = state.Lease.model_validate_json(current)
            if (lease.user_id, lease.owner_token) != (args[0], args[1]):
                return 0
            del self.values[key]
            return 1
        if script == state._DROP:
            if current is None or keys[1] in self.values:
                return 0
            remote = state.RemoteSession.model_validate_json(current)
            if remote.remote_id != args[0] or remote.last_used > self.now - 300:
                return 0
            del self.values[key]
            return 1
        if script == state._FORGET:
            if current is None or state.RemoteSession.model_validate_json(current).remote_id != args[0]:
                return 0
            del self.values[key]
            return 1
        if script == "return redis.call('EXISTS', KEYS[1])":
            return int(current is not None)
        raise AssertionError(f"Unexpected Redis script: {json.dumps(script)}")
