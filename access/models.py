from django.db import models


class Lock(models.Model):
    """A physical lock cylinder, identified by its SimonsVoss serial."""
    serial = models.CharField(max_length=32, primary_key=True)
    door_name = models.CharField(max_length=255, blank=True)
    room_number = models.CharField(max_length=64, blank=True)
    location = models.CharField(max_length=64, blank=True)   # Standort.Gebäude.Etage
    area = models.CharField(max_length=64, blank=True)       # Bereich

    class Meta:
        ordering = ["location", "door_name", "serial"]

    def __str__(self):
        return f"{self.serial} · {self.door_name}"

    @property
    def label(self):
        """Human label: door plus room number when it adds information."""
        base = self.door_name or self.serial
        room = self.room_number.strip()
        # Only append the room when the door has a name and does not already
        # carry that exact room token (substring would false-match e.g.
        # room '12' inside 'Labor 123').
        if room and self.door_name and room not in self.door_name.split():
            return f"{base} ({room})"
        return base


class Group(models.Model):
    """A reusable named door-set (e.g. "AStA Allgemein"). Assigning a group to
    a transponder adds its doors to that transponder's desired ("Soll") set."""
    name = models.CharField(max_length=128, unique=True)
    doors = models.ManyToManyField(Lock, related_name="groups", blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def label(self):
        return self.name


class Transponder(models.Model):
    """A transponder and the set of locks it may open."""
    serial = models.CharField(max_length=32, primary_key=True)
    asta_number = models.IntegerField(null=True, blank=True)
    person_name = models.CharField(max_length=255, blank=True)
    locking_system = models.CharField(max_length=64, blank=True)
    printed_on = models.DateField(null=True, blank=True)
    source_file = models.CharField(max_length=255, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)
    # Doors this transponder opens *now* (programmed / bold × in a locking matrix,
    # and every door on a per-transponder list printout, which shows current
    # rights).
    locks = models.ManyToManyField(Lock, related_name="transponders", blank=True)
    # Doors it will open once the change is written at the terminal (a thin ×
    # in a locking matrix — "erteilt sobald Update am Terminal"). Only a
    # matrix export distinguishes these; other sources leave it empty.
    planned_locks = models.ManyToManyField(
        Lock, related_name="planned_transponders", blank=True)
    # The target state we WANT programmed ("Soll") — curated in the admin or
    # seeded from the configured state. The diff export compares this wish
    # against the configured (active ∪ planned) state: matches are green,
    # doors that must still be added or removed are red.
    desired_locks = models.ManyToManyField(
        Lock, related_name="desired_transponders", blank=True)
    # Doors this transponder is currently PROGRAMMED to open but whose
    # authorisation was withdrawn in the source matrix (hollow outline × —
    # pending removal at the next terminal update). Part of the Ist-Zustand:
    # still physically openable now, but on its way out.
    removed_locks = models.ManyToManyField(
        Lock, related_name="removed_transponders", blank=True)
    # Groups this transponder belongs to. Membership is a convenience: the
    # effective Soll is desired_locks, kept in sync when a group is assigned /
    # unassigned or its doors change (see access/soll.py).
    groups = models.ManyToManyField(Group, related_name="transponders",
                                    blank=True)

    class Meta:
        ordering = ["asta_number", "person_name", "serial"]

    def __str__(self):
        return f"{self.serial} · {self.label}"

    @property
    def label(self):
        """Owner name if known, else the ASTA number, else the serial."""
        if self.person_name:
            return self.person_name
        if self.asta_number is not None:
            return f"ASTA {self.asta_number}"
        return self.serial

    @property
    def has_planned(self):
        """True when a terminal update is pending for this transponder."""
        return self.planned_locks.exists()
