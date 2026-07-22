"""Forms for creating / editing locks and transponders from the UI."""

from django import forms

from .models import Lock, Transponder

_INPUT = ("mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm "
          "focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100 outline-none")


# Serials that would collide with routing: "new" is the create route, and the
# <str:serial> URL converter rejects slashes (a slash would 500 on redirect).
_RESERVED_SERIALS = {"new"}


class _StyledModelForm(forms.ModelForm):
    """Apply the app's input styling to every widget; validate the serial."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            css = field.widget.attrs.get("class", "")
            field.widget.attrs["class"] = f"{css} {_INPUT}".strip()

    def clean_serial(self):
        serial = (self.cleaned_data.get("serial") or "").strip()
        if "/" in serial:
            raise forms.ValidationError("Seriennummer darf kein „/“ enthalten.")
        if serial.lower() in _RESERVED_SERIALS:
            raise forms.ValidationError("Diese Seriennummer ist reserviert.")
        return serial


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
