// The page an item link leaves behind. Once the pill has the item, this tab was only the link's
// way in, so it closes itself. Browsers allow that for a tab with nothing to go back to, which is
// what a link opened from another app gets; anywhere else it stays, saying where the item went.
if (document.body.hasAttribute('data-close')) window.close();
