import contextlib
import errno
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from spiritboard.cli import main, open_dashboard


class LauncherCliTests(unittest.TestCase):
    def test_bare_command_launches_without_requiring_a_subcommand(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("spiritboard.cli.Store") as store,
            patch("spiritboard.cli.Collector") as collector,
            patch("spiritboard.cli.Instance") as instance,
            patch("spiritboard.monitor.Monitor") as monitor,
            patch("pathlib.Path.home", return_value=Path(directory)),
            patch("webbrowser.open", return_value=True),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            instance.return_value.__enter__.return_value.owner = True
            collector.return_value.stopping.wait.return_value = True
            self.assertEqual(main([]), 0)
            store.assert_called_once()
            monitor.assert_called_once()

    def test_second_launch_reuses_without_reading_sources_or_key(self):
        with (
            patch("spiritboard.cli.Instance") as instance,
            patch("spiritboard.cli.Store") as store,
            patch("spiritboard.cli.summary_key") as key,
            patch("spiritboard.cli.open_dashboard") as browser,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            existing = instance.return_value.__enter__.return_value
            existing.owner = False
            existing.existing_url.return_value = "http://127.0.0.1:8766"
            self.assertEqual(main(["--summaries"]), 0)
            browser.assert_called_once_with("http://127.0.0.1:8766", enabled=True)
            store.assert_not_called()
            key.assert_not_called()

    def test_same_config_filename_with_changed_privacy_options_is_not_equivalent(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("spiritboard.cli.Instance") as instance,
            patch("spiritboard.cli.open_dashboard"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            existing = instance.return_value.__enter__.return_value
            existing.owner = False
            existing.existing_url.return_value = "http://127.0.0.1:8766"
            path = Path(directory) / "sources.json"
            profile = {
                "id": "test",
                "kind": "codex",
                "label": "Synthetic",
                "root": str(Path(directory) / "invented-profile"),
                "include_text": True,
            }
            path.write_text(json.dumps({"profiles": [profile]}))
            self.assertEqual(main(["--config", str(path)]), 0)
            old_settings = instance.call_args.args[1]
            profile["include_text"] = False
            path.write_text(json.dumps({"profiles": [profile]}))
            self.assertEqual(main(["--config", str(path)]), 0)
            self.assertNotEqual(instance.call_args.args[1], old_settings)

    def test_serve_uses_default_port_or_explicit_zero_and_stops_cleanly(self):
        for options, expected in (([], 8766), (["--port", "0"], 0)):
            with (
                self.subTest(options=options),
                tempfile.TemporaryDirectory() as directory,
                patch("spiritboard.cli.Instance") as instance,
                patch("spiritboard.cli.Store") as store,
                patch("spiritboard.cli.Collector") as collector,
                patch("spiritboard.cli.open_dashboard"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                instance.return_value.__enter__.return_value.owner = True
                collector.return_value.stopping.wait.return_value = True
                self.assertEqual(
                    main(["serve", "--no-discovery", "--data-dir", directory, *options]), 0
                )
                self.assertEqual(collector.call_args.kwargs["port"], expected)
                collector.return_value.server_close.assert_called_once()
                store.return_value.close.assert_called_once()

    def test_busy_default_port_falls_back_but_explicit_port_never_does(self):
        for explicit in (False, True):
            with (
                self.subTest(explicit=explicit),
                tempfile.TemporaryDirectory() as directory,
                patch("spiritboard.cli.Instance") as instance,
                patch("spiritboard.cli.Store") as store,
                patch("spiritboard.cli.Collector") as collector,
                patch("spiritboard.cli.open_dashboard"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                instance.return_value.__enter__.return_value.owner = True
                server = MagicMock()
                server.stopping.wait.return_value = True
                collector.side_effect = [OSError(errno.EADDRINUSE, "busy"), server]
                args = ["--no-discovery", "--no-open", "--data-dir", directory]
                self.assertEqual(main(args + (["--port", "8766"] if explicit else [])), explicit)
                self.assertEqual(collector.call_count, 1 if explicit else 2)
                store.return_value.close.assert_called_once()

    def test_no_open_and_browser_failure_are_nonfatal(self):
        with patch("webbrowser.open", return_value=False) as browser:
            open_dashboard("http://127.0.0.1:8766", enabled=False)
            browser.assert_not_called()
            with contextlib.redirect_stdout(io.StringIO()) as output:
                open_dashboard("http://127.0.0.1:8766", enabled=True)
            self.assertIn("Browser unavailable", output.getvalue())

    def test_prune_refuses_an_active_collector(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("spiritboard.cli.Instance") as instance,
            patch("spiritboard.cli.Store") as store,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            (Path(directory) / "instance.lock").touch()
            instance.return_value.__enter__.return_value.owner = False
            arguments = ["prune", "--older-than-days", "7", "--data-dir", directory]
            self.assertEqual(main(arguments), 1)
            store.assert_not_called()
            self.assertIn("Stop the dashboard", output.getvalue())

    def test_prune_never_creates_history_in_a_mistyped_directory(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            typo = Path(directory) / "typo"
            self.assertEqual(main(["prune", "--older-than-days", "7", "--data-dir", str(typo)]), 1)
            self.assertIn("No dashboard history", output.getvalue())
            self.assertFalse(typo.exists())

    def test_demo_targets_the_dashboard_running_on_its_data_directory(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("spiritboard.cli.running_url", return_value="http://127.0.0.1:54321") as find,
            patch("spiritboard.demo.run_demo", return_value={"runs": 5, "delivered": True}) as run,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(["demo", "--data-dir", directory, "--delay", "0"]), 0)
            find.assert_called_once_with(Path(directory))
            self.assertEqual(run.call_args.args[0], "http://127.0.0.1:54321")

    def test_serve_publishes_before_the_first_source_scan(self):
        order = []
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("spiritboard.cli.Store"),
            patch("spiritboard.cli.Collector") as collector,
            patch("spiritboard.cli.Instance") as instance,
            patch("spiritboard.monitor.Monitor") as monitor,
            patch("spiritboard.cli.open_dashboard"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            owner = instance.return_value.__enter__.return_value
            owner.owner = True
            owner.publish.side_effect = lambda *_: order.append("publish")
            monitor.return_value.scan_once.side_effect = lambda: order.append("scan")
            collector.return_value.stopping.wait.return_value = True
            self.assertEqual(main(["--data-dir", directory, "--no-open"]), 0)
        self.assertEqual(order, ["publish", "scan"])

    def test_version_has_no_monitoring_side_effects(self):
        with (
            patch("spiritboard.cli.Instance") as instance,
            contextlib.redirect_stdout(io.StringIO()) as output,
            self.assertRaises(SystemExit) as error,
        ):
            main(["--version"])
        self.assertEqual(error.exception.code, 0)
        self.assertIn("Spiritboard 0.1.0", output.getvalue())
        instance.assert_not_called()

    def test_help_still_lists_commands(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as error:
            main(["--help"])
        self.assertEqual(error.exception.code, 0)
        self.assertIn("discover", output.getvalue())


if __name__ == "__main__":
    unittest.main()
