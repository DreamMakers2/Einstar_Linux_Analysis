"""Profile store view and the profiles dialog.

The lifecycle module owns the rules; these tests check that the *description*
the UI shows is accurate and that a rollback performed through the view lands in
the store.  All mutations happen on a copy of the store, never on the real one.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

from tools.wizard import profile_view  # noqa: E402

STORE = Path(os.path.expanduser("~/einstar-private/calib-profiles"))


def _copy_store(tmp_path: Path) -> Path:
    if not STORE.is_dir():
        pytest.skip("no profile store available")
    dst = tmp_path / "store"
    shutil.copytree(STORE, dst)
    return dst


def test_list_serials_and_summary(tmp_path):
    store = _copy_store(tmp_path)
    serials = profile_view.list_serials(store)
    assert serials, "copied store has no serials"
    summary = profile_view.store_summary(store, serials[0])
    assert summary["serial"] == serials[0]
    assert summary["factory_present"] is True
    assert summary["active"]["state"] in ("factory", "validated")
    if summary["revisions"]:
        rev = summary["revisions"][0]
        assert rev["index"] >= 1
        assert isinstance(rev["verified"], bool)


def test_summary_reports_missing_store(tmp_path):
    assert profile_view.list_serials(tmp_path / "absent") == []


def test_rollback_through_the_view_lands_in_the_store(tmp_path):
    from tools.wizard import lifecycle
    store = _copy_store(tmp_path)
    serial = profile_view.list_serials(store)[0]
    before = profile_view.store_summary(store, serial)
    if before["active"]["state"] == "factory":
        pytest.skip("this store has no active revision to roll back")
    result = lifecycle.rollback(store, serial, None)
    assert result["state"] == "factory"
    after = profile_view.store_summary(store, serial)
    assert after["active"]["state"] == "factory"
    # the revision still exists after a rollback: rollback is not deletion
    assert len(after["revisions"]) == len(before["revisions"])


def test_profiles_dialog_lists_and_gates_the_buttons(tmp_path):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    import tools.app as launcher

    store = _copy_store(tmp_path)
    dialog = launcher.ProfilesDialog(str(store))
    try:
        serial = dialog.serial_box.currentText()
        assert serial and serial != "<none>"
        summary = profile_view.store_summary(store, serial)
        assert dialog.table.rowCount() == len(summary["revisions"])
        assert summary["active"]["state"] in dialog.status.text() or \
            "factory" in dialog.status.text()
        active_state = summary["active"]["state"]
        assert dialog.rollback_factory.isEnabled() == (active_state != "factory")
        assert dialog.rollback_sel.isEnabled() == (
            bool(summary["revisions"]) and active_state != "factory")
    finally:
        dialog.close()
        app.processEvents()
