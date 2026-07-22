"""Create the ASTA door-groups and transponder memberships from the wishlist.

    python manage.py seed_groups

One-off: turns the AStA Allgemein / Lager / Umweltlager / Technik / StudiTUM
General door-sets into Group records and assigns each transponder to its
groups. Idempotent — sets (not adds) each group's doors and each
transponder's memberships. desired_locks is left as-is (already seeded).
"""

import re

from django.core.management.base import BaseCommand
from django.db import transaction

from access.models import Group, Lock, Transponder

GROUP_DOORS = {
    "AStA Allgemein": [
        "Mensa ASTA Eingang", "Mensa ASTA Eingang Links", "Mensa ASTA Notausgang",
        "Mensa Gitterbox 2 Mülllager", "Bihinderten Eing. 010 Ost",
        "Haupteingang 008 West", "Raum 104 Stud. Arbeit", "Raum 308 Dusche",
        "Eingang -111 ( Keller )", "Raum - 103 Lager"],
    "AStA Lager": [
        "Mensa Raum -1004 ASTA Keller", "Mensa Vorhangschloss 1",
        "Mensa Vorhangschloss 2", "Notausgang Ost 001", "Notausgang Süd 012",
        "Eingang -111 ( Keller )", "Raum - 101 lager", "Raum - 102 ( Technik )",
        "Raum - 103 Lager", "Raum - 108 ( Technik )", "Raum - 110 ( Technik )",
        "Flur Bau 3 z. Bau 8 West", "Flur Bau 3 z. Bau 8 Ost",
        "A -1307 E Flur Ost zur Architekturmuseum",
        "A -1307 E Flur West zur Architekturmuseum", "A Raum 0307 zur Keller",
        "Raum -1818 b Säureraum", "Raum -1818 zur Halle",
        "Flur zu Bau 3 Ost", "Flur zu Bau 3 West"],
    "AStA Umweltlager": [
        "Bihinderten Eing. 010 Ost", "Haupteingang 008 West",
        "Eingang -111 ( Keller )", "Raum - 101 lager"],
    "AStA Technik": ["Raum -1607 A", "Raum -1607 ASTA Technikkeller"],
    "AStA StudiTUM General": [],   # whole G43 building, resolved below
}

CARD_GROUPS = {}
def _add(serials, groups):
    for s in serials.split():
        CARD_GROUPS[s] = groups
_add("0XEHF6 0XEHKA 0XGTD5 0XHHSC 0XKCGX 0XRFKE 106LDU 106NRT 10A0CS 10A112 10A1N6 10A2A2 10A2AS 10BB5L 10BF7C 10BMLP 1USCHF 1USN9R 1USNTP 1USRUH 1USSC2 1USTK6 1USUUT 1UTRPX 1UUTM5 1X0ALB 1XEUPT 1XNP2T 2UAA4K 2UAA55 2UBGL3 2UFSDU 2UFU56 2UL0C2 2UM6GX 2UP4BA 2URHK6 2XA73H 2XB29R 2XBC35 2XPKNR 3THFLX 3TR9UX 3TRE66 3TRUH3 3TS83N 3U02LU 3U3E6L 3UAHSN 3UAP0B 3UAR11 3UB9FL 3UCB91", ["AStA Allgemein"])
_add("2UEHS1 2UH7LR 2UKX0E 2XEU1B 3UAG03 3UCC4E", ["AStA Allgemein", "AStA Lager"])
_add("1USX8A 2UH4PG 2UL6P5 2XCT95 3TN2G5 3UCEAK", ["AStA Allgemein", "AStA Umweltlager"])
_add("0XKK2X 11ELMN 2UAAUR 2UAUH6 2UK9KC 3T5CTL 3U35MC 3U50U4", ["AStA Allgemein", "AStA Technik"])
_add("2UAC5U 2UEE9D 3U4345 3UB67H", ["AStA Allgemein", "AStA Lager", "AStA StudiTUM General"])


class Command(BaseCommand):
    help = "Create ASTA door-groups and transponder memberships from the wishlist."

    @transaction.atomic
    def handle(self, *args, **opts):
        def norm(s):
            return re.sub(r"\s+", " ", (s or "").strip()).casefold()
        by_name = {norm(l.door_name): l for l in Lock.objects.all()}
        g43 = [l for l in Lock.objects.all()
               if "g43" in ((l.location or "") + " " + (l.area or "")).lower()
               or "gab 43" in ((l.location or "") + " " + (l.area or "")).lower()]

        groups = {}
        for name, door_names in GROUP_DOORS.items():
            g, _ = Group.objects.get_or_create(name=name)
            doors = g43 if name == "AStA StudiTUM General" else [
                by_name[norm(dn)] for dn in door_names if norm(dn) in by_name]
            g.doors.set(doors)
            groups[name] = g
            self.stdout.write(f"  {name}: {g.doors.count()} doors")

        def resolve(s):
            for c in (s, "0" + s, "00" + s):
                if Transponder.objects.filter(serial=c).exists():
                    return c
            return None

        assigned = 0
        for s, gnames in CARD_GROUPS.items():
            rs = resolve(s)
            if rs is None:
                continue
            tp = Transponder.objects.get(serial=rs)
            gs = [groups[n] for n in gnames]
            tp.groups.set(gs)
            # Keep the invariant: a member's desired includes its groups' doors.
            for g in gs:
                tp.desired_locks.add(*g.doors.all())
            assigned += 1
        self.stdout.write(self.style.SUCCESS(
            f"Seeded {len(groups)} groups; assigned memberships to "
            f"{assigned} transponders."))
