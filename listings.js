// ─────────────────────────────────────────────────────────────────────────────
// SAMPLE LISTINGS MODULE — swap point for the live IDX / Flexmls feed.
//
// TWO independent modules live in this file:
//
//   window.HOT_LISTINGS      → the "Hot Homes" strip on the front page.
//                              Fed later by the agent's Flexmls hot sheet /
//                              saved search (the "specific list" he keeps).
//                              Swap point in index.html: <div id="hot-module">
//
//   window.SAMPLE_LISTINGS   → the "Featured Homes" grid.
//                              Swap point in index.html: <div id="listings-module">
//
// When the real feed exists, delete the array you're replacing and drop the
// vendor widget into that div — or keep these renderers and just repoint the
// data at the API. Nothing else on the page changes.
//
// Keep in sync with api/listings_data.py, which is what Sofia reads on calls.
// All listings below are FICTIONAL samples for demo purposes.
// ─────────────────────────────────────────────────────────────────────────────

// Hot sheet: new / price-improved / about-to-go. `hot` drives the flame ribbon.
window.HOT_LISTINGS = [
  {
    price: 385000,
    address: "1523 Cimarron Ridge Dr",
    area: "West Side · Cimarron",
    beds: 4, baths: 2.5, sqft: 2400,
    hot: { en: "Open House Sat", es: "Casa Abierta Sáb" },
    note: { en: "Listed 3 days ago · already 2 showings booked",
            es: "Publicada hace 3 días · ya con 2 citas" },
    img: "https://images.unsplash.com/photo-1600596542815-ffad4c1539a9?w=800&q=70&auto=format&fit=crop"
  },
  {
    price: 265000,
    address: "3308 Tierra Nocturna Ave",
    area: "East Side",
    beds: 3, baths: 2, sqft: 1780,
    hot: { en: "Price Drop −$14k", es: "Bajó $14k" },
    note: { en: "Seller motivated · reduced this week",
            es: "Vendedor motivado · rebaja esta semana" },
    img: "https://images.unsplash.com/photo-1570129477492-45c003edd2be?w=800&q=70&auto=format&fit=crop"
  },
  {
    price: 232000,
    address: "14208 Desert Sage Ct",
    area: "Horizon City",
    beds: 3, baths: 2, sqft: 1650,
    hot: { en: "Just Listed", es: "Recién Publicada" },
    note: { en: "Under $240k in Horizon — these move fast",
            es: "Menos de $240k en Horizon — se van rápido" },
    img: "https://images.unsplash.com/photo-1568605114967-8130f3a36994?w=800&q=70&auto=format&fit=crop"
  }
];

window.SAMPLE_LISTINGS = [
  {
    price: 489000,
    address: "6412 Camino Coronado",
    area: "Upper Valley",
    beds: 4, baths: 3, sqft: 2850,
    tag: { en: "New Listing", es: "Nueva" },
    img: "https://images.unsplash.com/photo-1564013799919-ab600027ffc6?w=800&q=70&auto=format&fit=crop"
  },
  {
    price: 385000,
    address: "1523 Cimarron Ridge Dr",
    area: "West Side · Cimarron",
    beds: 4, baths: 2.5, sqft: 2400,
    tag: { en: "Open House Sat", es: "Casa Abierta Sáb" },
    img: "https://images.unsplash.com/photo-1600596542815-ffad4c1539a9?w=800&q=70&auto=format&fit=crop"
  },
  {
    price: 265000,
    address: "3308 Tierra Nocturna Ave",
    area: "East Side",
    beds: 3, baths: 2, sqft: 1780,
    tag: { en: "Great Starter", es: "Ideal Primera Casa" },
    img: "https://images.unsplash.com/photo-1570129477492-45c003edd2be?w=800&q=70&auto=format&fit=crop"
  },
  {
    price: 549000,
    address: "912 Rim Rd",
    area: "Kern Place",
    beds: 5, baths: 3.5, sqft: 3300,
    tag: { en: "Luxury", es: "Lujo" },
    img: "https://images.unsplash.com/photo-1512917774080-9991f1c4c750?w=800&q=70&auto=format&fit=crop"
  },
  {
    price: 232000,
    address: "14208 Desert Sage Ct",
    area: "Horizon City",
    beds: 3, baths: 2, sqft: 1650,
    tag: { en: "Under $240k", es: "Menos de $240k" },
    img: "https://images.unsplash.com/photo-1568605114967-8130f3a36994?w=800&q=70&auto=format&fit=crop"
  },
  {
    price: 415000,
    address: "7625 Franklin Summit Dr",
    area: "Northeast · Mountain views",
    beds: 4, baths: 3, sqft: 2600,
    tag: { en: "Mountain Views", es: "Vista a la Montaña" },
    img: "https://images.unsplash.com/photo-1600585154340-be6161a56a0c?w=800&q=70&auto=format&fit=crop"
  }
];
