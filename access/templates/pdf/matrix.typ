// Tiled Schließmatrix. Data comes from data.json (written next to this file);
// page size from `--input size=a4|a3`, render mode from `--input mode=…`.
// The full transponders×doors grid is split into page-sized blocks, each a grid with
// rotated transponder headers on top and door labels down the left.
//
// mode "matrix": mark weight 2 = active (bold ×), 1 = planned (thin ×).
// mode "diff":   each mark is [weight, wished]; the cell is tinted green when
//                the configured state matches the wish and red when a door
//                must still be added (wished, weight 0) or removed (weight>0,
//                not wished).

#let data = json("data.json")
#let size = sys.inputs.at("size", default: "a3")
#let mode = sys.inputs.at("mode", default: "matrix")
#let a4 = size == "a4"
#let diff = mode == "diff"

#let (pw, ph) = if a4 { (595.28pt, 841.89pt) } else { (841.89pt, 1190.55pt) }
#let margin = 24pt
#let cell = 10pt
#let rowh = 11pt
#let labelw = 186pt
#let headerh = 150pt
#let heading-h = if diff { 34pt } else { 18pt }

#let cols-pp = calc.floor((pw - 2 * margin - labelw) / cell)
#let rows-pp = calc.floor((ph - 2 * margin - headerh - heading-h - 22pt) / rowh)

#let transponders = data.transponders
#let doors = data.doors
#let ntransponders = transponders.len()
#let ndoors = doors.len()
#let col-blocks = calc.max(1, calc.ceil(ntransponders / cols-pp))
#let row-blocks = calc.max(1, calc.ceil(ndoors / rows-pp))

// Colours
#let ok-bg = rgb("#bbf7d0")     // green-200 — configured as wished
#let bad-bg = rgb("#fecaca")    // red-200 — needs a change
#let add-col = rgb("#dc2626")   // red-600 — wished but not configured

// Font-independent cross.
#let cross(th, col) = {
  let s = 6.5pt
  box(width: s, height: s, {
    place(line(start: (0pt, 0pt), end: (s, s), stroke: th + col))
    place(line(start: (s, 0pt), end: (0pt, s), stroke: th + col))
  })
}
#let mark(w) = if w == 2 { cross(1.4pt, black) } else if w == 1 { cross(0.5pt, luma(140)) } else { [] }

// Grapheme-safe truncation (keeps umlauts intact).
#let trunc(s, n) = {
  let g = s.clusters()
  if g.len() > n { g.slice(0, n - 1).join() + "…" } else { s }
}

#let mode-label = if diff { "Soll/Ist-Vergleich" } else {
  (all: "aktiv + geplant", active: "nur aktiv", planned: "nur geplant")
    .at(data.scope, default: data.scope)
}

#set page(
  paper: if a4 { "a4" } else { "a3" },
  margin: margin,
  footer: context {
    set text(7pt, fill: luma(120))
    [#data.title — #mode-label · Stand #data.generated]
    h(1fr)
    [Seite #counter(page).display() / #counter(page).final().first()]
  },
)
#set text(font: ("Helvetica Neue", "Helvetica", "Arial"), size: 7pt)

#let legend = [
  #set text(6.5pt)
  #box(fill: ok-bg, inset: (x: 3pt, y: 1pt))[grün = programmiert wie gewünscht]
  #h(5pt)
  #box(fill: bad-bg, inset: (x: 3pt, y: 1pt))[rot = muss geändert werden]
  #h(8pt)
  #box(cross(1.4pt, black)) aktiv
  #h(4pt) #box(cross(0.5pt, luma(140))) geplant
  #h(4pt) #box(cross(0.9pt, add-col)) Soll fehlt
]

// One page for a (col-block × row-block) tile.
#let page-tile(cb, rb) = {
  let cs = cb * cols-pp
  let rs = rb * rows-pp
  let tblk = transponders.slice(cs, calc.min(cs + cols-pp, ntransponders))
  let dblk = doors.slice(rs, calc.min(rs + rows-pp, ndoors))
  let n = tblk.len()

  block(below: 6pt, {
    text(9pt)[*#data.title*]
    h(6pt)
    text(7pt, fill: luma(110))[Zeile #{rs + 1}–#{rs + dblk.len()} · Spalte #{cs + 1}–#{cs + n} · #ntransponders Transponder × #ndoors Türen]
    if diff {
      text(7pt)[#h(6pt) · #{data.counts.ok} passen, #text(fill: add-col)[#{data.counts.add} fehlen], #text(fill: add-col)[#{data.counts.remove} zu viel]]
      linebreak()
      legend
    }
  })

  let cells = ()
  cells.push(box(height: headerh, inset: 2pt,
    align(bottom + left, text(6pt, fill: luma(120))[NAME (TÜREN / SCHLIESSUNGEN)])))
  for c in tblk {
    cells.push(box(height: headerh, width: cell, inset: (bottom: 3pt),
      align(bottom + center,
        rotate(-90deg, reflow: true,
          box(width: headerh - 8pt, text(6.5pt)[#trunc(c.label, 34)])))))
  }
  for (i, d) in dblk.enumerate() {
    let loc = if d.location != "" [ #text(fill: luma(160))[· #trunc(d.location, 16)]] else []
    cells.push(box(width: labelw, height: rowh, inset: (left: 3pt),
      align(left + horizon, text(6.5pt)[#trunc(d.name, 40)#loc])))
    for (j, _c) in tblk.enumerate() {
      let key = str(cs + j) + "-" + str(rs + i)
      if diff {
        let v = data.marks.at(key, default: none)
        if v == none {
          cells.push(box(width: cell, height: rowh))
        } else {
          let weight = v.at(0)
          let wished = v.at(1)
          let bg = if weight > 0 and wished == 1 { ok-bg } else { bad-bg }
          let glyph = if weight == 2 { cross(1.4pt, black) } else if weight == 1 { cross(0.5pt, luma(120)) } else { cross(0.9pt, add-col) }
          cells.push(grid.cell(fill: bg, align(center + horizon, glyph)))
        }
      } else {
        let w = data.marks.at(key, default: 0)
        cells.push(box(width: cell, height: rowh, align(center + horizon, mark(w))))
      }
    }
  }

  grid(
    columns: (labelw, ..range(n).map(_ => cell)),
    rows: (headerh, ..range(dblk.len()).map(_ => rowh)),
    stroke: 0.3pt + luma(205),
    ..cells,
  )
}

#let first = true
#for rb in range(row-blocks) {
  for cb in range(col-blocks) {
    if not first { pagebreak() }
    first = false
    page-tile(cb, rb)
  }
}
