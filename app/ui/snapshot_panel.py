"""
SnapshotPanel — two-column tree view of named curve variants.

Column 0 (Name): badge + swatch + name, painted by `_SnapshotDelegate`.
Inline rename via double-click (Enter commits, Esc cancels, empty rejected).

Column 1 (Notes): plain text, edited via QTreeWidget's default item editor.
Inline edit via double-click on the notes cell. Active snapshot doesn't change
as a side effect of entering the notes editor (see _on_item_double_clicked).

Owns no snapshot state of its own; it renders and mutates an externally-owned
SnapshotCollection (held by MainWindow). Emits high-level signals that the
MainWindow uses to fan out a full refresh.
"""
from __future__ import annotations

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QTreeWidget, QTreeWidgetItem, QStyledItemDelegate, QLineEdit,
    QMessageBox, QGroupBox, QMenu, QStyleOptionViewItem, QAbstractItemView,
    QStyle, QHeaderView,
)
from PyQt6.QtCore import Qt, QRect, QRectF, pyqtSignal
from PyQt6.QtGui import QFont, QColor, QPainter, QPixmap, QIcon

from core.snapshot import SnapshotCollection, Snapshot


# Muted palette for snapshot color tags. Keys are stored in projects; hex values
# can be retuned freely without breaking file compatibility.
PALETTE_HEX = {
    "red":    "#c8645a",
    "orange": "#d49454",
    "yellow": "#c8b04a",
    "green":  "#6ab572",
    "cyan":   "#4aa8b8",
    "blue":   "#5a8ac8",
    "purple": "#9a7ab8",
    "pink":   "#c87a9a",
}

# Order shown in the right-click submenu — rainbow-ish for predictable scanning.
PALETTE_ORDER = ["red", "orange", "yellow", "green",
                 "cyan", "blue", "purple", "pink"]


def make_swatch_icon(hex_color: str, px: int = 12, swatch_px: int = 8) -> QIcon:
    """8×8 rounded-square swatch on a transparent backdrop. Shared with the
    render queue panel so the queue's Snapshot column reuses identical
    geometry and colors."""
    pix = QPixmap(px, px)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setBrush(QColor(hex_color))
    p.setPen(Qt.PenStyle.NoPen)
    inset = (px - swatch_px) / 2.0
    p.drawRoundedRect(QRectF(inset, inset, swatch_px, swatch_px), 2.0, 2.0)
    p.end()
    return QIcon(pix)


def make_blank_icon(px: int = 12) -> QIcon:
    """Transparent placeholder of the same footprint as the swatch icon so
    cells with no color don't shift the text x-offset."""
    pix = QPixmap(px, px)
    pix.fill(Qt.GlobalColor.transparent)
    return QIcon(pix)


# Custom data roles carried on each tree item so the delegate can paint the
# badge / swatch without a back-reference into the SnapshotCollection.
ROLE_SNAP_ID = Qt.ItemDataRole.UserRole
ROLE_ACTIVE  = Qt.ItemDataRole.UserRole + 1
ROLE_COLOR   = Qt.ItemDataRole.UserRole + 2

NAME_COL  = 0
NOTES_COL = 1


_TREE_QSS = (
    "QTreeWidget{background:#0e0e0f; border:1px solid #2a2a2e;"
    " color:#c8c8cc; font-family:monospace; font-size:10pt;}"
    "QTreeWidget::item{padding:0px; border:none;}"
    # Selection bg matches _SnapshotDelegate.BG_SELECTED so the row colour
    # is consistent whether painted by the delegate (col 0) or by Qt
    # default (col 1).
    "QTreeWidget::item:selected{background:#1a2a3a; color:#e8e8ec;}"
    "QHeaderView::section{background:#161618; color:#6a6a72;"
    " border:none; border-right:1px solid #2a2a2e;"
    " border-bottom:1px solid #2a2a2e; padding:3px 6px;"
    " font-family:monospace; font-size:9pt; letter-spacing:1px;}"
)


class _SnapshotDelegate(QStyledItemDelegate):
    """Paints col 0 (Name) as `[ ▶|·  ][ swatch|·  ][ name ]` with fixed
    column widths so rows stay aligned across active/inactive and color/
    no-color states. Inline rename editor: select-all on open; reject
    empty/whitespace on commit; geometry inset to clear the fixed columns.

    Not used for col 1 (Notes) — that column uses Qt's default rendering
    and editor."""

    BADGE_W  = 12
    SWATCH_W = 12
    SWATCH_PX = 8
    LEFT_PAD = 8
    TEXT_GAP = 4

    BG_SELECTED = QColor("#1a2a3a")
    BG_HOVER    = QColor("#161618")
    BG_BORDER   = QColor("#4a9eff")
    TEXT_ACTIVE = QColor("#e8e8ec")
    TEXT_NORMAL = QColor("#c8c8cc")

    # ── Inline rename support ─────────────────────────────────────────────────

    def setEditorData(self, editor, index):
        super().setEditorData(editor, index)
        if isinstance(editor, QLineEdit):
            editor.selectAll()

    def setModelData(self, editor, model, index):
        if isinstance(editor, QLineEdit) and not editor.text().strip():
            return  # reject — leave model untouched
        super().setModelData(editor, model, index)

    def updateEditorGeometry(self, editor, option, index):
        rect = QRect(option.rect)
        rect.setLeft(rect.left() + self.LEFT_PAD
                     + self.BADGE_W + self.SWATCH_W + self.TEXT_GAP)
        editor.setGeometry(rect)

    def sizeHint(self, option, index):
        sz = super().sizeHint(option, index)
        sz.setHeight(max(sz.height(), 22))
        return sz

    # ── Custom paint ──────────────────────────────────────────────────────────

    def paint(self, painter: QPainter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        rect = opt.rect

        is_selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        is_hover    = bool(opt.state & QStyle.StateFlag.State_MouseOver)
        is_active   = bool(index.data(ROLE_ACTIVE))

        painter.save()
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

            # 1. Background. Painted manually so we own the 2-px accent stripe
            # on the selected row (which QSS can't reliably render through
            # CE_ItemViewItem in QTreeWidget).
            if is_selected:
                painter.fillRect(rect, self.BG_SELECTED)
                painter.fillRect(QRect(rect.x(), rect.y(), 2, rect.height()),
                                 self.BG_BORDER)
            elif is_hover:
                painter.fillRect(rect, self.BG_HOVER)

            # 2. Badge column — ▶ for active, blank otherwise (same width).
            x = rect.x() + self.LEFT_PAD
            badge_rect = QRect(x, rect.y(), self.BADGE_W, rect.height())
            if is_active:
                painter.setFont(opt.font)
                painter.setPen(self.BG_BORDER)
                painter.drawText(badge_rect,
                                 int(Qt.AlignmentFlag.AlignVCenter
                                     | Qt.AlignmentFlag.AlignLeft),
                                 "▶")
            x += self.BADGE_W

            # 3. Color swatch column — 8x8 rounded square; blank if color None.
            color_key = index.data(ROLE_COLOR)
            if color_key and color_key in PALETTE_HEX:
                sw_x = x + (self.SWATCH_W - self.SWATCH_PX) / 2
                sw_y = rect.y() + (rect.height() - self.SWATCH_PX) / 2
                sw_rect = QRectF(sw_x, sw_y, self.SWATCH_PX, self.SWATCH_PX)
                painter.setBrush(QColor(PALETTE_HEX[color_key]))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawRoundedRect(sw_rect, 2.0, 2.0)
            x += self.SWATCH_W

            # 4. Name text.
            text_x = x + self.TEXT_GAP
            text_rect = QRect(text_x, rect.y(),
                              rect.right() - text_x - 4, rect.height())
            painter.setFont(opt.font)
            painter.setPen(self.TEXT_ACTIVE if is_selected else self.TEXT_NORMAL)
            name = index.data(Qt.ItemDataRole.DisplayRole) or ""
            # Elide if name overflows the name column's width.
            metrics = painter.fontMetrics()
            elided = metrics.elidedText(str(name), Qt.TextElideMode.ElideRight,
                                        text_rect.width())
            painter.drawText(text_rect,
                             int(Qt.AlignmentFlag.AlignVCenter
                                 | Qt.AlignmentFlag.AlignLeft),
                             elided)
        finally:
            painter.restore()


class SnapshotPanel(QWidget):
    # Fired when the user activates a different snapshot via this panel.
    activeChanged = pyqtSignal()
    # Fired when the snapshot list mutated (add / duplicate / delete / rename
    # / reorder / notes / color change) so the parent can persist if desired.
    listChanged   = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._snapshots: SnapshotCollection = SnapshotCollection()
        self._suppress_active_emit = False
        # Used by _on_item_double_clicked to revert active when the user
        # double-clicks the notes cell on a non-active row.
        self._prev_active_id: "str | None" = None
        self._build_ui()

    # ── UI build ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        box = QGroupBox("SNAPSHOTS")
        box_layout = QVBoxLayout(box)
        box_layout.setSpacing(6)
        box_layout.setContentsMargins(6, 8, 6, 6)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Name", "Notes"])
        self.tree.setRootIsDecorated(False)
        self.tree.setIndentation(0)
        self.tree.setUniformRowHeights(True)
        self.tree.setStyleSheet(_TREE_QSS)
        # Per-column edit triggers don't exist in Qt — gate manually in the
        # itemDoubleClicked handler. NoEditTriggers prevents the platform's
        # default (e.g. F2) from opening the wrong column.
        self.tree.setEditTriggers(QTreeWidget.EditTrigger.NoEditTriggers)
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self.tree.setItemDelegateForColumn(NAME_COL, _SnapshotDelegate(self.tree))
        # Column 1 (Notes) uses Qt's default delegate — plain text, default
        # QLineEdit editor with Esc=cancel built-in.
        self.tree.setMouseTracking(True)

        # Drag-to-reorder. InternalMove fires rowsMoved on the model after the
        # drop completes — we use that to sync the SnapshotCollection.
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.tree.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.tree.setDragEnabled(True)
        self.tree.setAcceptDrops(True)
        self.tree.setDropIndicatorShown(True)
        self.tree.model().rowsMoved.connect(self._on_rows_moved)

        # Header: name interactive, notes stretches.
        header = self.tree.header()
        header.setSectionResizeMode(NAME_COL,  QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(NOTES_COL, QHeaderView.ResizeMode.Stretch)
        header.setStretchLastSection(True)
        header.setSectionsMovable(False)
        # Default 40 / 60 split assuming a ~400 px-wide tree (panel 420 minus
        # group-box and tree borders). Real width settles after first show;
        # this is just the initial column 0 size.
        self.tree.setColumnWidth(NAME_COL, 160)

        # Right-click → color submenu.
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)

        self.tree.currentItemChanged.connect(self._on_current_row_changed)
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.tree.itemChanged.connect(self._on_item_changed)

        box_layout.addWidget(self.tree, stretch=1)

        # Button row
        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)
        self.btn_new       = QPushButton("+ New")
        self.btn_duplicate = QPushButton("Duplicate")
        self.btn_rename    = QPushButton("Rename")
        self.btn_delete    = QPushButton("Delete")
        for b in (self.btn_new, self.btn_duplicate, self.btn_rename, self.btn_delete):
            b.setFixedHeight(24)
            btn_row.addWidget(b)
        self.btn_new.clicked.connect(self._on_new_clicked)
        self.btn_duplicate.clicked.connect(self._on_duplicate_clicked)
        self.btn_rename.clicked.connect(self._on_rename_clicked)
        self.btn_delete.clicked.connect(self._on_delete_clicked)
        box_layout.addLayout(btn_row)

        outer.addWidget(box, stretch=1)

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def snapshots(self) -> SnapshotCollection:
        return self._snapshots

    def set_snapshots(self, snaps: SnapshotCollection):
        self._snapshots = snaps
        self._rebuild_list()

    def refresh(self):
        self._rebuild_list()

    def cycle(self, direction: int):
        """PageUp/PageDown handler. Returns the now-active snapshot, or None
        if the collection is empty (it shouldn't be in practice)."""
        if len(self._snapshots) == 0:
            return None
        snap = self._snapshots.cycle(direction)
        self._rebuild_list()
        self.activeChanged.emit()
        return snap

    # ── Internals ─────────────────────────────────────────────────────────────

    def _rebuild_list(self):
        """Re-populate the tree from `_snapshots`. Blocks signals so the
        programmatic selection change doesn't re-trigger activeChanged or
        itemChanged."""
        self.tree.blockSignals(True)
        try:
            self.tree.clear()
            bold = QFont(self.tree.font())
            bold.setBold(True)
            plain = QFont(self.tree.font())
            active_id = self._snapshots.active_id
            active_item = None
            for snap in self._snapshots.snapshots:
                item = QTreeWidgetItem([snap.name, snap.notes or ""])
                item.setData(NAME_COL, ROLE_SNAP_ID, snap.id)
                item.setData(NAME_COL, ROLE_ACTIVE,  snap.id == active_id)
                item.setData(NAME_COL, ROLE_COLOR,   snap.color)
                # Editable for inline rename (name) + notes edit; draggable for
                # reorder. Drop-target flag stays OFF so InternalMove treats
                # drops as "between rows" rather than "into a row".
                flags = item.flags() | Qt.ItemFlag.ItemIsEditable | Qt.ItemFlag.ItemIsDragEnabled
                flags &= ~Qt.ItemFlag.ItemIsDropEnabled
                item.setFlags(flags)
                # Per-column font: only the name column shows the bold-active
                # treatment so column 1 stays uniform weight.
                item.setFont(NAME_COL, bold if snap.id == active_id else plain)
                self.tree.addTopLevelItem(item)
                if snap.id == active_id:
                    active_item = item
            if active_item is not None:
                self.tree.setCurrentItem(active_item)
        finally:
            self.tree.blockSignals(False)
        self._update_delete_enabled()

    def _update_delete_enabled(self):
        n = len(self._snapshots)
        self.btn_delete.setEnabled(n > 1)
        if n <= 1:
            self.btn_delete.setToolTip("At least one snapshot must exist")
        else:
            self.btn_delete.setToolTip("")

    def _item_for_id(self, snap_id: str) -> "QTreeWidgetItem | None":
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            if it.data(NAME_COL, ROLE_SNAP_ID) == snap_id:
                return it
        return None

    def _row_for_id(self, snap_id: str) -> int:
        it = self._item_for_id(snap_id)
        if it is None:
            return -1
        return self.tree.indexOfTopLevelItem(it)

    # ── Signals from the tree ────────────────────────────────────────────────

    def _on_current_row_changed(self, current, previous):
        if current is None:
            return
        snap_id = current.data(NAME_COL, ROLE_SNAP_ID)
        if snap_id and snap_id != self._snapshots.active_id:
            # Stash the OLD active for the double-click-on-notes revert path.
            self._prev_active_id = self._snapshots.active_id
            self._snapshots.set_active(snap_id)
            self._restyle_active_rows(snap_id)
            if not self._suppress_active_emit:
                self.activeChanged.emit()

    def _restyle_active_rows(self, active_id: str):
        """Re-bold the new active row and clear bolding from previous ones.
        Updates ROLE_ACTIVE so the delegate redraws the badge in the right row.
        Block signals while mutating to avoid retrigger of itemChanged."""
        self.tree.blockSignals(True)
        try:
            bold = QFont(self.tree.font())
            bold.setBold(True)
            plain = QFont(self.tree.font())
            for i in range(self.tree.topLevelItemCount()):
                it = self.tree.topLevelItem(i)
                active_here = (it.data(NAME_COL, ROLE_SNAP_ID) == active_id)
                it.setFont(NAME_COL, bold if active_here else plain)
                it.setData(NAME_COL, ROLE_ACTIVE, active_here)
        finally:
            self.tree.blockSignals(False)

    def _on_item_double_clicked(self, item, column):
        """Per-column edit dispatch. The brief requires the notes editor to
        NOT change which snapshot is active, but Qt's mousePress already
        triggered the single-click activation by the time we get here. So:
        for notes column on a row that became active via the press, revert
        active to whatever it was before the press, THEN open the editor.

        For the name column, the single-click activation is desired and is
        kept."""
        if item is None or column not in (NAME_COL, NOTES_COL):
            return
        if column == NAME_COL:
            self.tree.editItem(item, NAME_COL)
            return
        # column == NOTES_COL
        snap_id = item.data(NAME_COL, ROLE_SNAP_ID)
        if (snap_id and self._prev_active_id is not None
                and self._prev_active_id != snap_id
                and self._prev_active_id != self._snapshots.active_id
                and snap_id == self._snapshots.active_id):
            # The single-click of this double-click switched active. Revert
            # so the editor opens without a lingering activation side-effect.
            prev = self._prev_active_id
            self._prev_active_id = None
            self._snapshots.set_active(prev)
            self._restyle_active_rows(prev)
            # Fan out so curve editor / dope sheet / viewer rebind to the
            # restored active. Use the public signal — listeners expect it.
            self.activeChanged.emit()
        self.tree.editItem(item, NOTES_COL)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int):
        """Dispatch to per-column commit handlers. Name column rejects empty
        (delegate-level guard); notes column accepts any string including
        empty."""
        snap_id = item.data(NAME_COL, ROLE_SNAP_ID)
        if not snap_id:
            return
        try:
            snap = self._snapshots.get(snap_id)
        except KeyError:
            return
        if column == NAME_COL:
            new_name = item.text(NAME_COL)
            if not new_name.strip():
                # Defensive: delegate should have rejected, but if a path
                # other than the delegate writes here, restore.
                self.tree.blockSignals(True)
                try:
                    item.setText(NAME_COL, snap.name)
                finally:
                    self.tree.blockSignals(False)
                return
            if snap.name == new_name:
                return
            snap.name = new_name
            self.listChanged.emit()
        elif column == NOTES_COL:
            new_notes = item.text(NOTES_COL)
            if snap.notes == new_notes:
                return
            snap.notes = new_notes
            self.listChanged.emit()

    def _on_rows_moved(self, parent, start, end, dest_parent, dest_row):
        """Sync the SnapshotCollection to the new visual order after a drop.
        Active id is unchanged; we just re-position by id."""
        widget_ids = [self.tree.topLevelItem(i).data(NAME_COL, ROLE_SNAP_ID)
                      for i in range(self.tree.topLevelItemCount())]
        for target_idx, sid in enumerate(widget_ids):
            try:
                cur_idx = next(i for i, s in enumerate(self._snapshots.snapshots)
                               if s.id == sid)
            except StopIteration:
                continue
            if cur_idx != target_idx:
                self._snapshots.move(sid, target_idx)
        self.listChanged.emit()

    # ── Context menu ──────────────────────────────────────────────────────────

    def _on_context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if item is None:
            return
        snap_id = item.data(NAME_COL, ROLE_SNAP_ID)
        if not snap_id:
            return
        try:
            current_color = self._snapshots.get(snap_id).color
        except KeyError:
            return

        menu = QMenu(self)
        color_menu = menu.addMenu("Color")
        for key in PALETTE_ORDER:
            act = color_menu.addAction(key.capitalize())
            act.setIcon(self._swatch_icon(PALETTE_HEX[key]))
            act.setCheckable(True)
            act.setChecked(current_color == key)
            act.triggered.connect(
                lambda checked=False, k=key, sid=snap_id: self._set_color(sid, k))
        color_menu.addSeparator()
        none_act = color_menu.addAction("None")
        none_act.setCheckable(True)
        none_act.setChecked(current_color is None)
        none_act.triggered.connect(
            lambda checked=False, sid=snap_id: self._set_color(sid, None))

        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _swatch_icon(self, hex_color: str) -> QIcon:
        # Submenu-icon variant uses a non-transparent backdrop so it reads
        # cleanly inside the menu's lighter chrome.
        pix = QPixmap(12, 12)
        pix.fill(QColor("#0e0e0f"))
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setBrush(QColor(hex_color))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(2, 2, 8, 8), 2.0, 2.0)
        p.end()
        return QIcon(pix)

    def _set_color(self, snap_id: str, color_key):
        try:
            snap = self._snapshots.get(snap_id)
        except KeyError:
            return
        if snap.color == color_key:
            return
        snap.color = color_key
        # Update the row's ROLE_COLOR in place so we don't lose selection focus.
        item = self._item_for_id(snap_id)
        if item is not None:
            self.tree.blockSignals(True)
            try:
                item.setData(NAME_COL, ROLE_COLOR, color_key)
            finally:
                self.tree.blockSignals(False)
            self.tree.viewport().update()
        self.listChanged.emit()

    # ── Button handlers ───────────────────────────────────────────────────────

    def _on_new_clicked(self):
        from core.timewarp import TimewarpCurve
        active = self._snapshots.active()
        curve = TimewarpCurve()
        curve.set_range(active.curve.in_start,
                        active.curve.in_end,
                        active.curve.out_start)
        curve.reset(1.0)
        snap = Snapshot(
            curve=curve,
            in_point=active.in_point,
            out_point=active.out_point,
            name=self._snapshots.default_new_name(),
        )
        self._snapshots.add(snap, make_active=True)
        self._rebuild_list()
        self.activeChanged.emit()
        self.listChanged.emit()
        self._begin_rename(snap.id)

    def _on_duplicate_clicked(self):
        if len(self._snapshots) == 0:
            return
        new_snap = self._snapshots.duplicate(self._snapshots.active_id,
                                             make_active=True)
        self._rebuild_list()
        self.activeChanged.emit()
        self.listChanged.emit()
        self._begin_rename(new_snap.id)

    def _on_rename_clicked(self):
        self._begin_rename(self._snapshots.active_id)

    def _on_delete_clicked(self):
        if len(self._snapshots) <= 1:
            return
        snap = self._snapshots.active()
        reply = QMessageBox.question(
            self, "Delete Snapshot",
            f"Delete snapshot '{snap.name}'? This cannot be undone.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._snapshots.remove(snap.id)
        self._rebuild_list()
        self.activeChanged.emit()
        self.listChanged.emit()

    def _begin_rename(self, snap_id: str):
        item = self._item_for_id(snap_id)
        if item is None:
            return
        self.tree.setCurrentItem(item)
        self.tree.editItem(item, NAME_COL)
