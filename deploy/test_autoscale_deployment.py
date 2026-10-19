import json
import unittest

from deploy.test_deployment import render
from deploy.test_resources import REQUIRED_INPUTS


class AutoscalerDeploymentTests(unittest.TestCase):
    def test_socket_when_default_state_directory_is_rendered_is_private_and_not_published(self) -> None:
        result = render(dict(REQUIRED_INPUTS))
        self.assertEqual(result.returncode, 0, result.stderr)
        ingress = json.loads(result.stdout)["services"]["ingress"]
        mounts = [mount for mount in ingress["volumes"]
                  if mount["target"] == "/run/maraclaw-autoscaler"]
        self.assertEqual(len(mounts), 1)
        self.assertEqual(mounts[0]["source"], "/var/lib/maraclaw-autoscaler")
        self.assertEqual(mounts[0]["type"], "bind")
        self.assertFalse(mounts[0].get("bind", {}).get("create_host_path", False))
        self.assertEqual(ingress["user"], "0:0")
        self.assertEqual({port["target"] for port in ingress["ports"]}, {8080, 8443})

    def test_socket_when_operator_overrides_state_directory_renders_exact_mount(self) -> None:
        values = dict(REQUIRED_INPUTS) | {"AUTOSCALER_STATE_DIR": "/srv/private-autoscaler"}
        result = render(values)
        self.assertEqual(result.returncode, 0, result.stderr)
        mounts = json.loads(result.stdout)["services"]["ingress"]["volumes"]
        self.assertEqual(next(mount["source"] for mount in mounts
                              if mount["target"] == "/run/maraclaw-autoscaler"),
                         "/srv/private-autoscaler")
