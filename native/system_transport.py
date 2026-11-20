"""Route operations to host-configured application instances by stable run-local name."""


class SystemTransport:
    def __init__(self):
        self._instances = {}

    def bind(self, name, system, adapter):
        """Install a trusted adapter without changing any existing instance binding."""
        if not all(isinstance(value, str) and value.strip() for value in (name, system)):
            raise ValueError("System and instance names must be nonblank strings")
        if name in self._instances:
            raise ValueError(f"Instance already bound: {name}")
        self._instances[name] = system, adapter

    def _resolve(self, name):
        try:
            return self._instances[name]
        except KeyError:
            raise ValueError(f"Unknown software instance: {name}") from None

    def find(self, name, retry_id):
        system, adapter = self._resolve(name)
        return adapter.find(system, retry_id)

    def send(self, name, retry_id, document):
        system, adapter = self._resolve(name)
        return adapter.send(system, retry_id, document)

    @staticmethod
    def _qualified(name, system, record):
        if record.get("system") != system:
            raise ValueError(f"Unexpected native source for instance {name}")
        return {**record, "system": name}

    def read(self, name, native_id):
        system, adapter = self._resolve(name)
        return self._qualified(name, system, adapter.read(system, native_id))

    def records(self):
        for name, (system, adapter) in self._instances.items():
            for row in adapter.records():
                yield self._qualified(name, system, row)
