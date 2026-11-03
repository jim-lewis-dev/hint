"""HINT's own fullscreen message/text window, run by distro Python for GTK."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description="HINT display window")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--text")
    source.add_argument("--file")
    args = parser.parse_args(argv)
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk, Gtk, Pango
        ready, unused = Gtk.init_check()
        if not ready:
            raise RuntimeError("no usable desktop session")
        text = args.text if args.text is not None else Path(args.file).read_text(encoding="utf-8", errors="replace")
        window = Gtk.Window(title="HINT" if args.text is not None else "HINT - " + Path(args.file).name)
        window.set_default_size(900, 600)
        window.set_border_width(32)
        window.connect("destroy", Gtk.main_quit)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        window.add(box)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        box.pack_start(scroller, True, True, 0)
        if args.text is not None:
            label = Gtk.Label(label=text)
            label.set_line_wrap(True)
            label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            label.set_justify(Gtk.Justification.CENTER)
            label.set_max_width_chars(48)
            label.set_selectable(True)
            label.set_margin_top(24)
            label.set_margin_bottom(24)
            label.modify_font(Pango.FontDescription("Sans 32"))
            scroller.add_with_viewport(label)
        else:
            view = Gtk.TextView()
            view.set_editable(False)
            view.set_cursor_visible(False)
            view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            view.get_buffer().set_text(text)
            view.modify_font(Pango.FontDescription("Monospace 18"))
            scroller.add(view)
        close = Gtk.Button(label="Dismiss")
        close.set_halign(Gtk.Align.CENTER)
        close.connect("clicked", lambda button: window.destroy())
        box.pack_start(close, False, False, 0)

        def key_pressed(widget, event):
            if event.keyval in (Gdk.KEY_Escape, Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                window.destroy()
                return True
            return False
        window.connect("key-press-event", key_pressed)
        window.fullscreen()
        window.show_all()
        Gtk.main()
        return 0
    except (ImportError, ValueError, OSError, RuntimeError) as exc:
        print(f"HINT display: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
