"""
This file is part of the Note Organizer add-on for Anki

Note rearranger module

Copyright: (c) Glutanimate 2017
           (c) ijgnd 2020
License: GNU AGPL, version 3 or later; https://www.gnu.org/licenses/agpl-3.0.en.html
"""

from pprint import pprint as pp

from anki.errors import AnkiError

from aqt import mw
from aqt.utils import tooltip
from anki.utils import intTime, ids2str

from .config import gc
from .consts import *
from .helpers import fields_to_fill_for_nonempty_front_template

class Rearranger:
    """Performs the actual database reorganization"""

    def __init__(self, browser=None, card=None):
        self.browser = browser
        self.mw = mw
        self.card = card  # card is not None only when called from the reviewer context menu - onReviewerOrgMenu
        self.nid_map = {}  # in rearrange: self.nid_map[nid] = new_nid


    def processNids(self, nids, start, moved, repos=False):
        """
        Main function

        Arguments:

        - nids:  list, unprocessed note IDs as strings,
                 including potential action prefixes
        - start: int, creation date of first note (first row in dialog) as UNIX timestamp
        - moved: list, nids that were interactively moved by the user
        - repos: boolean, whether to reposition due dates or not
        """
        
        # Full database sync required:
        try:
            self.mw.col.modSchema(check=True)
        except AnkiError:
            tooltip("Reorganization aborted.")
            return False
        # Create checkpoint
        self.mw.checkpoint("Reorganize notes")

        nids, deleted, created = self.processActions(nids)
        modified, nidlist = self.rearrange(nids, start, moved, created)

        if repos:
            self.reposition(nidlist)

        self.mw.col.reset()
        self.mw.reset()

        tooltip("Reorganization complete:<br>"
            "<b>{}</b> note(s) <b>moved</b><br>"
            "<b>{}</b> note(s) <b>deleted</b><br>"
            "<b>{}</b> note(s) <b>created</b><br>"
            "<b>{}</b> note(s) <b>updated alongside</b><br>".format(
                len(moved), len(deleted), len(created), 
                len(modified)-len(moved)),
            parent=self.browser)

        to_select = moved + created
        if self.browser:
            self.selectNotes(self.browser, to_select)

        return(to_select)


    def first_valid_nid_in_nids_list(self, nids):
        """Find valid nid in nids list"""
        """original name: findSample"""
        curr = None
        for nid in nids:
            try:
                curr = int(nid)
                if self.noteExists(curr):
                    break
            except ValueError:
                continue
        return curr


    def processActions(self, nids):
        """
        Parse and execute actions in nid list (e.g. note creation)
        Also converts nids to ints
        TODO: Find a more elegant solution to pass commands from
              the Organizer to the Rearranger
        """
        processed = []
        deleted = []
        created = []

        for idx, nid in enumerate(nids):
            #if I insert a new note in the gui from the browser nid is "New: Same note type as previous"
            try:
                # Regular NID, no action
                processed.append(int(nid))
                continue
            except ValueError:
                vals = nid.split(": ")

            try:
                nxt = int(nids[idx+1])
            except (IndexError, ValueError):
                # last Index
                nxt = None

            action = vals[0]  # e.g. "New" (=NEW_NOTE)
            data = vals[1:]   # e.g. "Same note type as previous" (=MODEL_SAME)
            if action == DEL_NOTE:
                # Actions: Delete
                nnid = int(data[0])
                if not nnid or not self.noteExists(nnid):
                    continue
                self.removeNote(nnid)
                deleted.append(nnid)
                continue
            elif action.startswith((NEW_NOTE, DUPE_NOTE)):
                # Actions: New, Dupe, Dupe with Scheduling
                sched = False
                ntype = None
                if action.startswith(DUPE_NOTE):
                    neighboring_nid = int(data[0])  # dialog.onDuplicateNote: this is the nid of the note before the dupe (when the dupe was inserted)
                    sched = action == DUPE_NOTE_SCHED
                else:  # NEW_NOTE
                    ntype = "".join(data)
                    neighboring_nid = nxt or self.first_valid_nid_in_nids_list(nids)  # next nid in dialog
                if not neighboring_nid or not self.noteExists(neighboring_nid):
                    continue
                nid = self.addNote(neighboring_nid, ntype=ntype, sched=sched)
                if not nid:
                    continue
                created.append(int(nid))
                processed.append(int(nid))

        return processed, deleted, created


    def rearrange(self, nids, start, moved, created):
        """Adjust nid order"""
        modified = []
        nidlist = []
        alterated = moved + created
        last = 0

        for idx, nid in enumerate(nids):
            try:
                nxt = int(nids[idx+1])
            except (IndexError, ValueError):
                nxt = nid + 1

            if not self.noteExists(nid): # note deleted
                continue

            print("------------------------------")
            print(("last", last))
            print(("current", nid))
            print(("next", nxt))
            print(("nextmoved", nxt in moved))
            print(("expected", last < nid < nxt))
            # check if order as expected
            if last != 0 and last < nid < nxt:
                if nid in alterated and nxt in alterated:
                    print("moved block")
                    pass
                else:
                    print("skipping")
                    last = nid
                    nidlist.append(nid)
                    continue

            if last != 0:
                new_nid = last + 1 # regular nids
            elif start and start != (nid // 1000):
                new_nid = start * 1000 # first nid, date changed
            else:
                print("skipping first nid")
                last = nid # first nid, date unmodified
                nidlist.append(nid)
                continue

            print("modifying")
            
            
            new_nid = self.updateNidSafely(nid, new_nid)

            if nid not in created:
                modified.append(new_nid)
                idnote = False
            else:
                idnote = True

            self.setNidFields(new_nid, nid, idnote=idnote)

            # keep track of moved nids (e.g. for dupes)
            self.nid_map[nid] = new_nid
            
            print(("new_nid", new_nid))
            nidlist.append(new_nid)
            last = new_nid

        return modified, nidlist


    def addNote(self, neighbNid, ntype=None, sched=False):
        """
        Create new note based on a neighboring nid: This is used as the source note from which
        the new note will inherit: 
           - For empty new notes it seems to be the following note
           - for dupes it seems to be the preceeding note
        """
        neighbNid = self.nid_map.get(neighbNid, neighbNid)
        sourceNote = self.mw.col.getNote(neighbNid)
        
        if not self.card:   # self.card only if called from the reviewer - so this condition is True if called from the browser
            sourceCids = self.mw.col.db.list(
                    "select id from cards where nid = ? order by ord", neighbNid)
            try:
                visible_source_cid = sourceCids[0]
            except IndexError:
                # invalid state: note has no cards
                return None
            
            # try to use visible card if available
            if self.browser:
                for cid in sourceCids:
                    if cid in self.browser.model.cards:
                        visible_source_cid = cid
                        break
            
            source_card = self.mw.col.getCard(visible_source_cid)
        else:
            source_card = self.card
        
        # gather model/deck information
        source_did = source_card.odid or source_card.did # account for dyn decks
        source_deck = self.mw.col.decks.get(source_did)

        if not ntype or ntype == MODEL_SAME:
            model = sourceNote.model()
        else:
            model = self.mw.col.models.byName(ntype)
        
        # Assign model to deck
        self.mw.col.decks.select(source_did)
        source_deck['mid'] = model['id']
        self.mw.col.decks.save(source_deck)
        # Assign deck to model
        self.mw.col.models.setCurrent(model)
        model['did'] = source_did
        self.mw.col.models.save(model)
        
        # Create new note
        new_note = self.mw.col.newNote()
        new_note.tags = sourceNote.tags
        if not ntype: # dupe
            fields = sourceNote.fields
        else:
            # original solution: fill all fields to avoid notes without cards
            #    fields = ["."] * len(new_note.fields)
            # problem: That's a hassle for note types that generate e.g. up to 20 cards ...
            # for details see helpers.py
            fields = [""] * len(new_note.fields)
            for i in fields_to_fill_for_nonempty_front_template(new_note.mid):
                fields[i] = "."
        new_note.fields = fields
        if gc("BACKUP_FIELD") in new_note: # skip onid field
            new_note[gc("BACKUP_FIELD")] = ""
        
        # Refresh note and add to database
        new_note.flush()
        self.mw.col.addNote(new_note)

        # Copy over scheduling from old cards
        if sched:
            scards = sourceNote.cards()
            ncards = new_note.cards()
            for orig, copy in zip(scards, ncards):
                self.copyCardScheduling(orig, copy)

        return new_note.id


    def copyCardScheduling(self, o, c):
        """Copy scheduling data over from original card"""
        self.mw.col.db.execute(
            "update cards set type=?, queue=?, due=?, ivl=?, "
            "factor=?, reps=?, lapses=?, left=? where id = ?",
            o.type, o.queue, o.due, o.ivl,
            o.factor, o.reps, o.lapses, o.left, c.id)

    
    def removeNote(self, nid):
        self.mw.col.remNotes([nid])


    def noteExists(self, nid):
        """Checks the database to see whether the nid is actually assigned"""
        return self.mw.col.db.scalar(
            """select id from notes where id = ?""", nid)


    def updateNidSafely(self, nid, new_nid):
        """Update nid while ensuring that timestamp doesn't already exist"""
        while self.noteExists(new_nid):
            new_nid += 1

        # Leave some room for future changes when possible
        for i in range(20):
            new_nid += 1
            if self.noteExists(new_nid):
                new_nid -= 1
                break

        # Update note row
        self.mw.col.db.execute(
            """update notes set id=? where id = ?""", new_nid, nid)

        # Update card rows
        self.mw.col.db.execute(
            """update cards set nid=? where nid = ?""", new_nid, nid)

        return new_nid


    def setNidFields(self, nid, onid, idnote=False):
        """Store original NID in a predefined field (if available)"""
        note = self.mw.col.getNote(nid)
        if gc("BACKUP_FIELD") in note and not note[gc("BACKUP_FIELD")]:
            note[gc("BACKUP_FIELD")] = str(onid)
        if idnote and gc("nids_NID_FIELD_overwrite") in note: # add nid to note id field
            note["Note ID"] = str(nid)
        note.flush()


    def reposition(self, nidlist):
        cids = self.mw.col.db.list(
            "select id from cards where type = 0 and nid in " + ids2str(nidlist))
        if not cids:
            return
        self.mw.col.sched.sortCards(
            cids, start=0, step=1, shuffle=False, shift=True)


    def selectNotes(self, browser, nids):
        """Select browser entries by note id"""
        browser.form.tableView.selectionModel().clear()
        cids = []
        for nid in nids:
            nid = self.nid_map.get(nid, nid)
            cids += self.mw.col.db.list(
                "select id from cards where nid = ? order by ord", nid)
        browser.model.selectedCards = {cid: True for cid in cids}
        browser.model.restoreSelection()
