import ast
import http.client
import io
import pathlib
import socket
import types
import unittest
import xmlrpc.client


ROOT = pathlib.Path(__file__).resolve().parents[1]
CHIEF_PATH = ROOT / "zslurm_chief"


def load_selected_nodes(*names):
    tree = ast.parse(CHIEF_PATH.read_text(encoding="utf-8"))
    selected = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in names
            for target in node.targets
        ):
            selected.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in names:
            selected.append(node)
    module = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
    namespace = {
        "httplib": http.client,
        "socket": socket,
        "sys": types.SimpleNamespace(stderr=io.StringIO()),
        "xmlrpclib": xmlrpc.client,
        "xtime": lambda: "now",
    }
    exec(compile(module, str(CHIEF_PATH), "exec"), namespace)
    return namespace


class ChiefProtocolErrorTests(unittest.TestCase):
    def test_protocol_error_is_a_recoverable_manager_rpc_error(self):
        namespace = load_selected_nodes("MANAGER_RPC_ERRORS")

        self.assertIn(
            xmlrpc.client.ProtocolError,
            namespace["MANAGER_RPC_ERRORS"],
        )

    def test_protocol_error_during_unregister_does_not_mask_shutdown(self):
        namespace = load_selected_nodes("MANAGER_RPC_ERRORS", "safe_unregister")

        class MissingEndpoint:
            def unregister(self, _engine_id):
                raise xmlrpc.client.ProtocolError(
                    "http://manager/old-path", 404, "Not Found", {}
                )

        namespace["safe_unregister"](MissingEndpoint(), "engine-1")

        self.assertIn(
            "Manager unavailable during unregister",
            namespace["sys"].stderr.getvalue(),
        )

    def test_instance_reconnect_registers_on_redirected_uri(self):
        namespace = load_selected_nodes("reconnect_to_instance")
        namespace["current_instance_manager_uri"] = (
            lambda _instance: "http://manager/new-path"
        )
        namespace["register_at_manager"] = (
            lambda uri: ("new-proxy", "new-engine")
        )

        self.assertEqual(
            namespace["reconnect_to_instance"](
                "zslurm_fcn8", "http://manager/old-path"
            ),
            (
                "new-proxy",
                "new-engine",
                "http://manager/new-path",
            ),
        )

    def test_legacy_two_exception_handlers_are_gone(self):
        source = CHIEF_PATH.read_text(encoding="utf-8")

        self.assertNotIn(
            "except (socket.error, httplib.HTTPException)",
            source,
        )


if __name__ == "__main__":
    unittest.main()
