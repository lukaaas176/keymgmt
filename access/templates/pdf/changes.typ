// Reprogramming worklist — every outstanding change as a flat, per-transponder
// list. Data comes from data.json (written next to this file); page size from
// `--input size=a4|a3`. Each transponder block lists the doors to REMOVE (red −)
// and to ADD (green +) to reach its wished ("Soll") state.

#let data = json("data.json")
#let size = sys.inputs.at("size", default: "a4")
#let a4 = size != "a3"
#let (pw, ph) = if a4 { (595.28pt, 841.89pt) } else { (841.89pt, 1190.55pt) }

#let add-col = rgb("#16a34a")   // green-600 — must be added
#let rem-col = rgb("#dc2626")   // red-600 — must be removed
#let muted = luma(120)

#set page(
  paper: if a4 { "a4" } else { "a3" },
  margin: 34pt,
  footer: context {
    set text(7pt, fill: muted)
    [#data.title · Stand #data.generated]
    h(1fr)
    [Seite #counter(page).display() / #counter(page).final().first()]
  },
)
#set text(font: ("Helvetica Neue", "Helvetica", "Arial"), size: 9pt)

// Grapheme-safe truncation (keeps umlauts intact).
#let trunc(s, n) = {
  let g = s.clusters()
  if g.len() > n { g.slice(0, n - 1).join() + "…" } else { s }
}

// Header + summary.
#block(below: 12pt, {
  text(17pt, weight: "bold")[#data.title]
  linebreak()
  v(2pt)
  text(9.5pt, fill: muted)[
    #data.counts.transponders Transponder betroffen · #h(3pt)
    #text(fill: add-col, weight: "bold")[+#data.counts.add hinzufügen] · #h(3pt)
    #text(fill: rem-col, weight: "bold")[−#data.counts.remove entfernen] · #h(3pt)
    Stand #data.generated
  ]
})

#if data.changes.len() == 0 {
  align(center + horizon, text(12pt, fill: muted)[
    Keine ausstehenden Änderungen — alle Transponder entsprechen ihrem Soll.
  ])
}

// One change line: symbol · door name (+ optional note) · location (right).
#let door-line(sym, col, d, note) = grid(
  columns: (13pt, 1fr, auto),
  gutter: 5pt,
  align: (center, left, right),
  text(fill: col, weight: "bold")[#sym],
  {
    trunc(d.name, 64)
    if note != "" [ #text(fill: muted, size: 7pt)[ (#note)]]
  },
  text(fill: muted, size: 7.5pt)[#trunc(d.location, 22)],
)

#for (i, t) in data.changes.enumerate() {
  block(above: if i == 0 { 0pt } else { 10pt }, breakable: true, {
    // transponder heading bar
    block(fill: luma(238), inset: (x: 6pt, y: 4pt), radius: 3pt, width: 100%, {
      text(weight: "bold")[#trunc(t.label, 52)]
      h(6pt)
      text(fill: muted, size: 8pt)[#t.serial]
      if t.asta != none { text(fill: muted, size: 8pt)[ · ASTA #t.asta] }
      h(1fr)
      if t.remove.len() > 0 { text(fill: rem-col, size: 8pt, weight: "bold")[−#t.remove.len()] }
      if t.add.len() > 0 { text(fill: add-col, size: 8pt, weight: "bold")[  +#t.add.len()] }
    })
    v(3pt)
    pad(left: 4pt, {
      for d in t.remove { door-line("−", rem-col, d, d.note) }
      for d in t.add { door-line("+", add-col, d, "") }
    })
  })
}
