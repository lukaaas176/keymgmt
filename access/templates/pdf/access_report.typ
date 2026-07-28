// Export-group access report. All grouping and ordering comes from data.json;
// this template owns only A4 typography and pagination.

#let data = json("data.json")
#let muted = luma(115)
#let soft = luma(242)
#let accent = rgb("#4f46e5")

#set page(
  paper: "a4",
  margin: (x: 38pt, top: 40pt, bottom: 42pt),
  footer: context {
    set text(7pt, fill: muted)
    [Zugangsübersicht · Stand #data.generated]
    h(1fr)
    [Seite #counter(page).display() / #counter(page).final().first()]
  },
)
#set text(font: ("Helvetica Neue", "Helvetica", "Arial"), size: 9pt)
#set par(leading: 4pt)

#let location-list(locations) = {
  for location in locations {
    block(above: 5pt, below: 2pt, breakable: true, {
      text(8.5pt, weight: "semibold", fill: accent)[#location.name]
      for lock in location.locks {
        linebreak()
        h(8pt)
        [• #lock.label]
      }
    })
  }
}

#let serial-list(serials) = {
  for serial in serials {
    text(font: ("Courier", "Courier New"), size: 8.5pt)[#serial]
    linebreak()
  }
}

#text(18pt, weight: "bold")[#data.title]
#linebreak()
#v(3pt)
#text(8.5pt, fill: muted)[Stand #data.generated]
#v(14pt)

#if data.sections.len() == 0 {
  text(fill: muted, style: "italic")[Keine Exportgruppen.]
}

#for (index, section) in data.sections.enumerate() {
  block(above: if index == 0 { 0pt } else { 13pt }, breakable: true, {
    block(fill: soft, inset: (x: 7pt, y: 5pt), radius: 3pt, width: 100%, {
      text(13pt, weight: "bold")[#section.title]
    })
    v(6pt)
    text(9pt, weight: "bold")[Türen]
    if section.locations.len() == 0 {
      linebreak()
      text(fill: muted, style: "italic")[Keine Gruppentüren.]
    } else {
      location-list(section.locations)
    }
    v(6pt)
    text(9pt, weight: "bold")[Transponder]
    linebreak()
    serial-list(section.serials)
  })
}

#if data.sections.len() > 0 { pagebreak() }

#text(16pt, weight: "bold")[Zusätzliche individuelle Türen]
#v(10pt)

#if data.individuals.len() == 0 {
  text(fill: muted, style: "italic")[Keine zusätzlichen individuellen Türen.]
}

#for (index, individual) in data.individuals.enumerate() {
  block(above: if index == 0 { 0pt } else { 12pt }, breakable: true, {
    text(11pt, weight: "bold")[#individual.title]
    location-list(individual.locations)
  })
}
