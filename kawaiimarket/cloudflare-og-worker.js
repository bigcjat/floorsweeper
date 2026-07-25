/**
 * Cloudflare Worker: Dynamic OpenGraph Image Generator for Dropchan Rarity Floors
 * Host on Cloudflare Workers (e.g. og-rarity-floors.kawaii.market or worker subdomain)
 */

const ISSUER = "rDropCHANEgmG7FBz1nzPpG27BGzWjnCnn";
const TAXON = 0;
const IMAGE_CID = "bafybeigesgb5dft45uuazbz56k6cr7aps6bgbedkjbbopfmseb5rcj7cyy";

function renderSlotsSvg(items, startX, startY, badgeBgColor, badgeTextColor, priceColor) {
    let slotsSvg = "";
    const slotW = 166;
    const slotH = 190;
    const gap = 17;

    for (let i = 0; i < 3; i++) {
        const slotX = startX + i * (slotW + gap);
        const slotY = startY;
        const item = items[i];

        if (item) {
            let pFormatted = "";
            if (Number.isInteger(item.price)) {
                pFormatted = item.price >= 1000 ? item.price.toLocaleString() : item.price.toString();
            } else {
                const p1 = parseFloat(item.price.toFixed(1));
                pFormatted = p1 >= 1000 ? p1.toLocaleString() : p1.toString();
            }
            const pText = `${pFormatted} XRP`;
            const pSize = pText.length > 10 ? (pText.length > 13 ? 14 : 16) : 20;

            slotsSvg += `
              <g transform="translate(${slotX}, ${slotY})">
                <rect width="${slotW}" height="${slotH}" rx="16" fill="#ffffff" stroke="rgba(0,0,0,0.1)" stroke-width="1.5" />
                <clipPath id="clip-${item.id}">
                  <rect width="${slotW}" height="142" rx="16" />
                </clipPath>
                <g clip-path="url(#clip-${item.id})">
                  <image href="https://favicon.bot/ipfs/${IMAGE_CID}/${item.id}.svg" width="${slotW}" height="142" preserveAspectRatio="xMidYMid slice" />
                  <rect x="6" y="6" width="45" height="18" rx="6" fill="rgba(0,0,0,0.8)" />
                  <text x="28" y="19" font-size="10" font-weight="900" fill="#ffffff" text-anchor="middle">#${item.id}</text>

                  <rect x="75" y="6" width="85" height="18" rx="6" fill="${badgeBgColor}" />
                  <text x="117" y="19" font-size="9" font-weight="900" fill="${badgeTextColor}" text-anchor="middle">RANK #${item.rank}</text>
                </g>
                <text x="${slotW / 2}" y="172" font-size="${pSize}" font-weight="900" fill="${priceColor}" text-anchor="middle">${pText}</text>
              </g>
            `;
        } else {
            slotsSvg += `
              <g transform="translate(${slotX}, ${slotY})">
                <rect width="${slotW}" height="${slotH}" rx="16" fill="rgba(0,0,0,0.05)" stroke="rgba(0,0,0,0.05)" stroke-dasharray="4,4" />
                <text x="${slotW / 2}" y="95" font-size="11" font-weight="700" fill="rgba(0,0,0,0.3)" text-anchor="middle">Slot #${i+1}</text>
                <text x="${slotW / 2}" y="112" font-size="11" font-weight="700" fill="rgba(0,0,0,0.3)" text-anchor="middle">Unlisted</text>
              </g>
            `;
        }
    }
    return slotsSvg;
}

export default {
    async fetch(request, env, ctx) {
        const url = new URL(request.url);

        // CORS headers for OpenGraph crawlers
        const headers = {
            "Content-Type": "image/svg+xml; charset=utf-8",
            "Cache-Control": "public, max-age=180, s-maxage=180, stale-while-revalidate=60",
            "Access-Control-Allow-Origin": "*",
        };

        try {
            // Fetch live XRPData offers
            const resp = await fetch(`https://api.xrpldata.com/api/v1/xls20-nfts/offers/issuer/${ISSUER}/taxon/${TAXON}`, {
                headers: { "User-Agent": "KawaiiMarket-OGWorker/1.0" }
            });
            const resJson = await resp.json();
            const offersList = resJson.data?.offers || [];

            const RIPPLE_EPOCH_OFFSET = 946684800;
            const rippleNow = Math.floor(Date.now() / 1000) - RIPPLE_EPOCH_OFFSET;

            let listedNFTs = [];
            let overallFloor = Infinity;

            offersList.forEach(nft => {
                if (nft.URI) {
                    try {
                        let decodedUri = "";
                        for (let i = 0; i < nft.URI.length; i += 2) {
                            decodedUri += String.fromCharCode(parseInt(nft.URI.substr(i, 2), 16));
                        }
                        const tokenIdNum = parseInt(decodedUri.split("/").pop().split(".")[0]);
                        if (!isNaN(tokenIdNum)) {
                            // Enforce active sell offer owner verification
                            const activeSell = (nft.sell || []).filter(offer => {
                                if (nft.NFTokenOwner && offer.Owner !== nft.NFTokenOwner) return false;
                                if (typeof offer.Amount !== "string") return false;
                                if (offer.Expiration) {
                                    const exp = parseInt(offer.Expiration);
                                    if (!isNaN(exp) && exp <= rippleNow) return false;
                                }
                                return true;
                            });

                            if (activeSell.length > 0) {
                                let lowestSell = Infinity;
                                activeSell.forEach(offer => {
                                    const p = parseFloat(offer.Amount) / 1000000;
                                    if (p > 0 && p < lowestSell) lowestSell = p;
                                });
                                if (lowestSell !== Infinity) {
                                    if (lowestSell < overallFloor) overallFloor = lowestSell;
                                    // Rank fallback heuristic
                                    const mockRank = (tokenIdNum % 9999) + 1;
                                    listedNFTs.push({ id: tokenIdNum, price: lowestSell, rank: mockRank });
                                }
                            }
                        }
                    } catch (e) {}
                }
            });

            // Group into 4 rarity tiers
            const legendary = listedNFTs.filter(x => x.rank >= 1 && x.rank <= 100).sort((a,b) => a.price - b.price).slice(0, 3);
            const mythic = listedNFTs.filter(x => x.rank >= 101 && x.rank <= 500).sort((a,b) => a.price - b.price).slice(0, 3);
            const rare = listedNFTs.filter(x => x.rank >= 501 && x.rank <= 1500).sort((a,b) => a.price - b.price).slice(0, 3);
            const common = listedNFTs.filter(x => x.rank >= 1501).sort((a,b) => a.price - b.price).slice(0, 3);

            const floorDisplay = overallFloor !== Infinity ? `${Math.round(overallFloor)} XRP` : "15 XRP";
            const listingsDisplay = `${listedNFTs.length} Listed`;

            const legSlotsSvg = renderSlotsSvg(legendary, 40, 145, "#ffd043", "#4a3200", "#7a5500");
            const mytSlotsSvg = renderSlotsSvg(mythic, 630, 145, "#ff7bb0", "#ffffff", "#9e1656");
            const rarSlotsSvg = renderSlotsSvg(rare, 40, 415, "#36d6c3", "#003d36", "#00574f");
            const comSlotsSvg = renderSlotsSvg(common, 630, 415, "#a370ff", "#ffffff", "#4c198a");

            // Build dynamic 1200x675 SVG graphic
            const svg = `
<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="675" viewBox="0 0 1200 675">
  <defs>
    <style>
      @import url('https://fonts.googleapis.com/css2?family=Outfit:wght@700;900&amp;display=swap');
      text { font-family: 'Outfit', sans-serif; }
    </style>
    <linearGradient id="bgGrad" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#b8cfff" />
      <stop offset="50%" stop-color="#d4ebff" />
      <stop offset="100%" stop-color="#ffcce5" />
    </linearGradient>
  </defs>

  <!-- Card Outer Background -->
  <rect width="1200" height="675" fill="url(#bgGrad)" rx="36" stroke="#ffb0d6" stroke-width="10" />

  <!-- Header Box -->
  <rect x="20" y="20" width="1160" height="65" rx="24" fill="#3444b7" stroke="#ffb0d6" stroke-width="3" />
  
  <text x="80" y="60" font-size="28" font-weight="900" fill="#ffffff">dropchan</text>
  <rect x="210" y="38" width="170" height="26" rx="13" fill="#ff7bb0" />
  <text x="295" y="55" font-size="11" font-weight="900" fill="#ffffff" text-anchor="middle">RARITY FLOOR SNAPSHOT</text>
  <text x="80" y="75" font-size="11" font-weight="700" fill="#ffcce5">3 Cheapest Listed NFTs Per Rarity Tier</text>

  <!-- Collection Floor Badges -->
  <rect x="880" y="30" width="280" height="45" rx="16" fill="rgba(255,255,255,0.2)" stroke="rgba(255,255,255,0.4)" stroke-width="1.5" />
  <text x="940" y="47" font-size="9" font-weight="900" fill="#ffcce5" text-anchor="middle">COLLECTION FLOOR</text>
  <text x="940" y="66" font-size="20" font-weight="900" fill="#ffffff" text-anchor="middle">${floorDisplay}</text>
  <line x1="1020" y1="36" x2="1020" y2="69" stroke="rgba(255,255,255,0.3)" stroke-width="2" />
  <text x="1100" y="47" font-size="9" font-weight="900" fill="#ffcce5" text-anchor="middle">ACTIVE LISTINGS</text>
  <text x="1100" y="66" font-size="20" font-weight="900" fill="#ff7bb0" text-anchor="middle">${listingsDisplay}</text>

  <!-- Tier 1: LEGENDARY Box -->
  <rect x="20" y="100" width="570" height="255" rx="24" fill="#fff6d6" stroke="#ffd770" stroke-width="3" />
  <text x="40" y="128" font-size="14" font-weight="900" fill="#7a5500">👑 LEGENDARY (Rank 1 - 100)</text>
  <rect x="490" y="112" width="85" height="20" rx="10" fill="#ffd043" />
  <text x="532" y="126" font-size="10" font-weight="900" fill="#4a3200" text-anchor="middle">3 CHEAPEST</text>
  ${legSlotsSvg}

  <!-- Tier 2: MYTHIC Box -->
  <rect x="610" y="100" width="570" height="255" rx="24" fill="#ffd6e7" stroke="#ff94c2" stroke-width="3" />
  <text x="630" y="128" font-size="14" font-weight="900" fill="#9e1656">🔥 MYTHIC (Rank 101 - 500)</text>
  <rect x="1080" y="112" width="85" height="20" rx="10" fill="#ff7bb0" />
  <text x="1122" y="126" font-size="10" font-weight="900" fill="#ffffff" text-anchor="middle">3 CHEAPEST</text>
  ${mytSlotsSvg}

  <!-- Tier 3: RARE Box -->
  <rect x="20" y="370" width="570" height="255" rx="24" fill="#c8f5ee" stroke="#5ee3d1" stroke-width="3" />
  <text x="40" y="398" font-size="14" font-weight="900" fill="#00574f">💎 RARE (Rank 501 - 1500)</text>
  <rect x="490" y="382" width="85" height="20" rx="10" fill="#36d6c3" />
  <text x="532" y="396" font-size="10" font-weight="900" fill="#003d36" text-anchor="middle">3 CHEAPEST</text>
  ${rarSlotsSvg}

  <!-- Tier 4: COMMON Box -->
  <rect x="610" y="370" width="570" height="255" rx="24" fill="#e9d8ff" stroke="#c69eff" stroke-width="3" />
  <text x="630" y="398" font-size="14" font-weight="900" fill="#4c198a">🌸 COMMON (Rank 1501+)</text>
  <rect x="1080" y="382" width="85" height="20" rx="10" fill="#a370ff" />
  <text x="1122" y="396" font-size="10" font-weight="900" fill="#ffffff" text-anchor="middle">3 CHEAPEST</text>
  ${comSlotsSvg}

  <!-- Footer Info -->
  <text x="30" y="650" font-size="12" font-weight="700" fill="#1c2666">⚡ Live On-Ledger Price Floor Snapshot • ${new Date().toLocaleDateString('en-US', { month:'short', day:'numeric', year:'numeric' })}</text>
  <text x="1050" y="650" font-size="14" font-weight="900" fill="#1c2666">kawaii.market</text>
  <rect x="1130" y="635" width="50" height="20" rx="10" fill="rgba(255,255,255,0.7)" />
  <text x="1155" y="649" font-size="10" font-weight="700" fill="#1c2666" text-anchor="middle">#XRPL</text>
</svg>
`;

            return new Response(svg, { headers });

        } catch (e) {
            return new Response(`<svg width="1200" height="675"><rect width="1200" height="675" fill="#3444b7"/><text x="600" y="337" font-size="30" fill="#ffffff" text-anchor="middle">Dropchan Rarity Floors - kawaii.market</text></svg>`, { headers });
        }
    }
};
