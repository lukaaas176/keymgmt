"""Forms for creating / editing locks and transponders from the UI."""

from django import forms

from .models import Lock, Transponder

_INPUT = ("mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm "
          "focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100 outline-none")


class _StyledModelForm(forms.ModelForm):
    """Apply the app's input styling to every widget."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            css = field.widget.attrs.get("class", "")
            field.widget.attrs["class"] = f"{css} {_INPUT}".strip()


class LockForm(_StyledModelForm):
    class Meta:
        model = Lock
        fields = ["serial", "door_name", "room_number", "location", "area"]
        labels = {"serial": "Seriennr.", "door_name": "Tür",
                  "room_number": "Raum", "location": "Standort",
                  "area": "Bereich"}


class TransponderForm(_StyledModelForm):
    class Meta:
        model = Transponder
        fields = ["serial", "asta_number", "person_name", "locking_system",
                  "printed_on"]
        labels = {"serial": "Seriennr.", "asta_number": "ASTA-Nr.",
                  "person_name": "Inhaber", "locking_system": "Schließanlage",
                  "printed_on": "Ausdruck vom"}
        widgets = {"printed_on": forms.DateInput(attrs={"type": "date"},
                                                 format="%Y-%m-%d")}
