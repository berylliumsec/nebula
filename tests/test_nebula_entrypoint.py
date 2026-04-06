from nebula import nebula


def test_main_starts_application_and_exits(monkeypatch):
    instances = []
    exit_codes = []

    class FakeApp:
        def __init__(self, argv):
            self.argv = argv
            self.started = False
            instances.append(self)

        def start(self):
            self.started = True

        def exec(self):
            return 7

    monkeypatch.setattr(nebula, "MainApplication", FakeApp)
    monkeypatch.setattr(nebula.sys, "argv", ["nebula", "--demo"])
    monkeypatch.setattr(nebula.sys, "exit", exit_codes.append)

    nebula.main()

    assert len(instances) == 1
    assert instances[0].argv == ["nebula", "--demo"]
    assert instances[0].started is True
    assert exit_codes == [7]
