"""Check display initialization when both GTK/GDK generations are installed."""
from contextlib import redirect_stderr
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import hint_popup


class PopupTests(unittest.TestCase):
    def test_popup_and_text_document_load_matching_gtk_bindings(self):
        for arguments in (["--text=This is NOT A DRILL!!!"], ["--file=message.txt"]):
            with self.subTest(arguments=arguments):
                required = {}
                loaded = {}
                gtk = types.SimpleNamespace(init_check=Mock(return_value=(False, [])))
                namespaces = {"Gtk": gtk, "Gdk": types.SimpleNamespace(), "Pango": types.SimpleNamespace()}

                def require_version(name, version):
                    if name in loaded and loaded[name] != version:
                        raise ValueError(f"{name} {loaded[name]} is already loaded; cannot load {version}")
                    required[name] = version

                def load_namespace(name):
                    if name not in namespaces:
                        raise AttributeError(name)
                    if name not in loaded:
                        version = required.get(name, "1.0" if name == "Pango" else "4.0")
                        if name == "Gtk":
                            # GTK loads the matching GDK, but cannot replace an
                            # incompatible GDK imported earlier in the process.
                            require_version("Gdk", version)
                            load_namespace("Gdk")
                        loaded[name] = version
                    return namespaces[name]

                gi = types.ModuleType("gi")
                gi.__path__ = []
                gi.require_version = require_version
                repository = types.ModuleType("gi.repository")
                repository.__path__ = []
                repository.__getattr__ = load_namespace
                gi.repository = repository
                errors = io.StringIO()
                with patch.dict(sys.modules, {"gi": gi, "gi.repository": repository}):
                    with redirect_stderr(errors):
                        result = hint_popup.main(arguments)

                gtk.init_check.assert_called_once_with()
                self.assertEqual(loaded, {"Gdk": "3.0", "Gtk": "3.0", "Pango": "1.0"})
                self.assertEqual(result, 1)
                self.assertEqual(errors.getvalue(), "HINT display: no usable desktop session\n")


class GtkPopupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get("REQUIRE_GTK_TESTS") != "1":
            raise unittest.SkipTest("GTK window checks run explicitly under Xvfb in CI")
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            raise RuntimeError("GTK window test needs a desktop or Xvfb")
        cls.python = "/usr/bin/python3"
        if not Path(cls.python).is_file():
            raise RuntimeError("GTK window test needs distro Python")
        # Probe in a separate process: preloading GTK here would hide the
        # incompatible GDK import that caused the popup startup failure.
        probe = subprocess.run([
            cls.python, "-c",
            "import gi; gi.require_version('Gtk', '3.0'); gi.require_version('Gdk', '4.0')"
        ], capture_output=True, text=True, timeout=10)
        if probe.returncode:
            raise AssertionError(probe.stderr)

    def check_window(self, arguments, expected):
        script = """
import sys
import gi
gi.require_version('GLib', '2.0')
from gi.repository import GLib
sys.path.insert(0, sys.argv[1])
import hint_popup

def check_window():
    # Only import Gtk after the actual popup has initialized its bindings.
    from gi.repository import Gtk
    def texts(widget):
        if isinstance(widget, Gtk.Label):
            yield widget.get_text()
        if isinstance(widget, Gtk.TextView):
            buffer = widget.get_buffer()
            yield buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        if isinstance(widget, Gtk.Container):
            for child in widget.get_children():
                yield from texts(child)
    try:
        windows = [window for window in Gtk.Window.list_toplevels() if window.get_visible()]
        assert len(windows) == 1, windows
        assert sys.argv[2] in list(texts(windows[0]))
        print('HINT window displayed', flush=True)
        windows[0].destroy()
    finally:
        Gtk.main_quit()
    return False

GLib.idle_add(check_window)
raise SystemExit(hint_popup.main(sys.argv[3:]))
"""
        result = subprocess.run([
            self.python, "-c", script, str(Path(hint_popup.__file__).resolve().parent),
            expected, *arguments
        ], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("HINT window displayed", result.stdout, result.stderr)

    def test_popup_creates_a_visible_window_with_literal_text(self):
        text = "This is NOT A DRILL!!! <b>literal</b>"
        self.check_window(["--text=" + text], text)

    def test_text_file_creates_a_visible_window_with_its_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "message.txt"
            text = "First line\nSecond line"
            path.write_text(text, encoding="utf-8")
            self.check_window(["--file=" + str(path)], text)


if __name__ == "__main__":
    unittest.main()
