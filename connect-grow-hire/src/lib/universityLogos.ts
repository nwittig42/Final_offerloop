// University logo library for the school pill on person cards.
//
// Each school has a match pattern for the free-text school names that come
// back from search and the ranker ("University of Southern California",
// "USC Marshall School of Business", "usc"), its web domain, and its brand
// color (used to highlight the pill when the person went to the student's
// school). Schools with a mark in src/assets use it; the rest load their
// site icon through the same favicon service CompanyLogo uses, and the pill
// falls back to the school's name if that fails.
//
// Order matters: the first entry that matches wins, so schools whose names
// contain another school's name ("Michigan State" vs "Michigan",
// "Washington University" vs "University of Washington") carry an exclude
// pattern or sit above the broader entry.
import uscLogo from "@/assets/USClogo.png";
import uclaLogo from "@/assets/UCLAlogo.png";
import berkeleyLogo from "@/assets/Callogo.png";
import ucsdLogo from "@/assets/UCSDlogo.png";
import sdsuLogo from "@/assets/SDSUlogo.png";
import usdLogo from "@/assets/USDlogo.png";
import uciLogo from "@/assets/UCIlogo.png";
import ucsbLogo from "@/assets/UCSBlogo.png";
import caltechLogo from "@/assets/Caltechlogo.png";
import lmuLogo from "@/assets/LMUlogo.png";
import uwLogo from "@/assets/Udublogo.png";
import oregonLogo from "@/assets/UofOlogo.png";
import wsuLogo from "@/assets/WSUlogo.png";
import michiganLogo from "@/assets/Michigan logo.png";
import notreDameLogo from "@/assets/Notre Dame logo.png";
import stanfordLogo from "@/assets/Stanford logo.avif";

export interface UniversityLogo {
  key: string;
  name: string;
  match: RegExp;
  exclude?: RegExp;
  domain: string;
  color: string;
  asset?: string;
}

export const UNIVERSITY_LOGOS: UniversityLogo[] = [
  // ── California + West (local marks) ─────────────────────────────────
  { key: "usc", name: "USC", match: /\busc\b|southern california/, exclude: /south carolina|upstate/, domain: "usc.edu", color: "#990000", asset: uscLogo },
  { key: "ucla", name: "UCLA", match: /\bucla\b|california,? los angeles|uc los angeles/, domain: "ucla.edu", color: "#2774AE", asset: uclaLogo },
  { key: "berkeley", name: "UC Berkeley", match: /berkeley|haas school/, exclude: /berklee/, domain: "berkeley.edu", color: "#003262", asset: berkeleyLogo },
  { key: "sdsu", name: "San Diego State", match: /\bsdsu\b|san diego state/, domain: "sdsu.edu", color: "#A6192E", asset: sdsuLogo },
  { key: "ucsd", name: "UC San Diego", match: /\bucsd\b|uc san diego|california,? san diego/, domain: "ucsd.edu", color: "#182B49", asset: ucsdLogo },
  { key: "usd", name: "University of San Diego", match: /university of san diego|^usd$/, domain: "sandiego.edu", color: "#003B70", asset: usdLogo },
  { key: "uci", name: "UC Irvine", match: /\buci\b|uc irvine|california,? irvine/, domain: "uci.edu", color: "#0064A4", asset: uciLogo },
  { key: "ucsb", name: "UC Santa Barbara", match: /\bucsb\b|santa barbara/, domain: "ucsb.edu", color: "#003660", asset: ucsbLogo },
  { key: "caltech", name: "Caltech", match: /caltech|california institute of technology/, domain: "caltech.edu", color: "#FF6C0C", asset: caltechLogo },
  { key: "lmu", name: "LMU", match: /\blmu\b|loyola marymount/, domain: "lmu.edu", color: "#AB0C2F", asset: lmuLogo },
  { key: "stanford", name: "Stanford", match: /stanford/, domain: "stanford.edu", color: "#8C1515", asset: stanfordLogo },
  { key: "wustl", name: "WashU", match: /washington university|\bwustl\b|\bwashu\b/, exclude: /george washington|central washington|eastern washington|western washington/, domain: "wustl.edu", color: "#A51417" },
  { key: "wsu", name: "Washington State", match: /washington state|\bwsu\b/, domain: "wsu.edu", color: "#981E32", asset: wsuLogo },
  { key: "uw", name: "University of Washington", match: /university of washington|\budub\b|^uw$|uw seattle|foster school of business/, domain: "washington.edu", color: "#4B2E83", asset: uwLogo },
  { key: "oregon", name: "University of Oregon", match: /university of oregon|^oregon$/, domain: "uoregon.edu", color: "#154733", asset: oregonLogo },
  { key: "ucdavis", name: "UC Davis", match: /uc davis|california,? davis/, domain: "ucdavis.edu", color: "#022851" },
  { key: "calpoly", name: "Cal Poly", match: /cal poly|california polytechnic/, domain: "calpoly.edu", color: "#154734" },
  { key: "santaclara", name: "Santa Clara", match: /santa clara university/, domain: "scu.edu", color: "#862633" },
  { key: "pepperdine", name: "Pepperdine", match: /pepperdine/, domain: "pepperdine.edu", color: "#00205C" },
  { key: "cmc", name: "Claremont McKenna", match: /claremont mckenna/, domain: "cmc.edu", color: "#981A31" },
  { key: "pomona", name: "Pomona", match: /pomona college/, domain: "pomona.edu", color: "#0057B8" },
  { key: "asu", name: "Arizona State", match: /arizona state/, domain: "asu.edu", color: "#8C1D40" },
  { key: "arizona", name: "University of Arizona", match: /university of arizona/, domain: "arizona.edu", color: "#AB0520" },
  { key: "colorado", name: "CU Boulder", match: /university of colorado|cu boulder/, domain: "colorado.edu", color: "#CFB87C" },

  // ── Ivy League + Northeast ──────────────────────────────────────────
  { key: "harvard", name: "Harvard", match: /harvard/, domain: "harvard.edu", color: "#A51C30" },
  { key: "yale", name: "Yale", match: /\byale\b/, domain: "yale.edu", color: "#00356B" },
  { key: "princeton", name: "Princeton", match: /princeton/, domain: "princeton.edu", color: "#E77500" },
  { key: "pennstate", name: "Penn State", match: /penn state|pennsylvania state/, domain: "psu.edu", color: "#041E42" },
  { key: "penn", name: "Penn", match: /^(the )?university of pennsylvania|\bupenn\b|wharton/, domain: "upenn.edu", color: "#011F5B" },
  { key: "columbia", name: "Columbia", match: /columbia university|^columbia\b|columbia business school/, exclude: /british columbia|columbia college chicago/, domain: "columbia.edu", color: "#1D4F91" },
  { key: "cornell", name: "Cornell", match: /cornell/, domain: "cornell.edu", color: "#B31B1B" },
  { key: "brown", name: "Brown", match: /brown university|^brown$/, domain: "brown.edu", color: "#4E3629" },
  { key: "dartmouth", name: "Dartmouth", match: /dartmouth|tuck school/, domain: "dartmouth.edu", color: "#00693E" },
  { key: "mit", name: "MIT", match: /\bmit\b|massachusetts institute of technology|mit sloan/, domain: "mit.edu", color: "#A31F34" },
  { key: "nyu", name: "NYU", match: /\bnyu\b|new york university|stern school/, domain: "stern.nyu.edu", color: "#57068C" },
  { key: "fordham", name: "Fordham", match: /fordham/, domain: "fordham.edu", color: "#860038" },
  { key: "bc", name: "Boston College", match: /boston college/, domain: "bc.edu", color: "#8A100B" },
  { key: "bu", name: "Boston University", match: /boston university|^bu$/, domain: "bu.edu", color: "#CC0000" },
  { key: "northeastern", name: "Northeastern", match: /northeastern university|^northeastern$/, domain: "northeastern.edu", color: "#D41B2C" },
  { key: "tufts", name: "Tufts", match: /tufts/, domain: "tufts.edu", color: "#3E5E9A" },
  { key: "babson", name: "Babson", match: /babson/, domain: "babson.edu", color: "#006644" },
  { key: "bentley", name: "Bentley", match: /bentley university/, domain: "bentley.edu", color: "#003DA5" },
  { key: "williams", name: "Williams", match: /williams college/, domain: "williams.edu", color: "#500082" },
  { key: "amherst", name: "Amherst", match: /amherst college/, domain: "amherst.edu", color: "#3F1F69" },
  { key: "villanova", name: "Villanova", match: /villanova/, domain: "villanova.edu", color: "#00205B" },
  { key: "lehigh", name: "Lehigh", match: /lehigh/, domain: "lehigh.edu", color: "#653818" },
  { key: "rutgers", name: "Rutgers", match: /rutgers/, domain: "rutgers.edu", color: "#CC0033" },
  { key: "cmu", name: "Carnegie Mellon", match: /carnegie mellon|\bcmu\b/, domain: "cmu.edu", color: "#C41230" },
  { key: "jhu", name: "Johns Hopkins", match: /johns hopkins/, domain: "jhu.edu", color: "#002D72" },
  { key: "umd", name: "Maryland", match: /university of maryland/, domain: "umd.edu", color: "#E21833" },
  { key: "georgetown", name: "Georgetown", match: /georgetown/, domain: "georgetown.edu", color: "#041E42" },

  // ── South ───────────────────────────────────────────────────────────
  { key: "duke", name: "Duke", match: /\bduke\b|fuqua/, domain: "duke.edu", color: "#012169" },
  { key: "unc", name: "UNC", match: /unc chapel hill|university of north carolina|^unc$|kenan-flagler/, exclude: /charlotte|wilmington|greensboro|asheville|pembroke/, domain: "unc.edu", color: "#4B9CD3" },
  { key: "wakeforest", name: "Wake Forest", match: /wake forest/, domain: "wfu.edu", color: "#9E7E38" },
  { key: "uva", name: "UVA", match: /university of virginia|\buva\b|darden school/, domain: "virginia.edu", color: "#232D4B" },
  { key: "vanderbilt", name: "Vanderbilt", match: /vanderbilt/, domain: "vanderbilt.edu", color: "#866D4B" },
  { key: "emory", name: "Emory", match: /emory/, domain: "emory.edu", color: "#012169" },
  { key: "gatech", name: "Georgia Tech", match: /georgia tech|georgia institute of technology/, domain: "gatech.edu", color: "#B3A369" },
  { key: "uga", name: "UGA", match: /university of georgia/, domain: "uga.edu", color: "#BA0C2F" },
  { key: "umiami", name: "Miami", match: /university of miami/, domain: "welcome.miami.edu", color: "#F47321" },
  { key: "fsu", name: "Florida State", match: /florida state/, domain: "fsu.edu", color: "#782F40" },
  { key: "uf", name: "Florida", match: /university of florida|^uf$/, domain: "ufl.edu", color: "#0021A5" },
  { key: "utaustin", name: "UT Austin", match: /ut austin|university of texas,? (at )?austin|^university of texas$|mccombs/, domain: "utexas.edu", color: "#BF5700" },
  { key: "tamu", name: "Texas A&M", match: /texas a&m|\btamu\b/, domain: "tamu.edu", color: "#500000" },
  { key: "rice", name: "Rice", match: /rice university/, domain: "rice.edu", color: "#00205B" },
  { key: "smu", name: "SMU", match: /southern methodist|\bsmu\b/, domain: "smu.edu", color: "#C8102E" },
  { key: "tcu", name: "TCU", match: /texas christian|\btcu\b/, domain: "tcu.edu", color: "#4D1979" },
  { key: "baylor", name: "Baylor", match: /baylor/, domain: "baylor.edu", color: "#154734" },

  // ── Midwest ─────────────────────────────────────────────────────────
  { key: "msu", name: "Michigan State", match: /michigan state/, domain: "msu.edu", color: "#18453B" },
  { key: "michigan", name: "Michigan", match: /university of michigan|\bumich\b|^michigan$|ross school of business/, exclude: /dearborn|flint/, domain: "umich.edu", color: "#00274C", asset: michiganLogo },
  { key: "notredame", name: "Notre Dame", match: /notre dame/, domain: "nd.edu", color: "#0C2340", asset: notreDameLogo },
  { key: "northwestern", name: "Northwestern", match: /northwestern university|^northwestern$|kellogg school of management/, domain: "northwestern.edu", color: "#4E2A84" },
  { key: "uchicago", name: "UChicago", match: /university of chicago|\buchicago\b|booth school/, domain: "uchicago.edu", color: "#800000" },
  { key: "illinois", name: "Illinois", match: /university of illinois|\buiuc\b|urbana-champaign|gies college/, exclude: /chicago|springfield/, domain: "illinois.edu", color: "#E84A27" },
  { key: "wisconsin", name: "Wisconsin", match: /university of wisconsin|uw.?madison/, exclude: /milwaukee|eau claire|la crosse|oshkosh|whitewater|stout|green bay|parkside|platteville|river falls|stevens point|superior/, domain: "wisc.edu", color: "#C5050C" },
  { key: "indiana", name: "Indiana", match: /indiana university|kelley school/, exclude: /pennsylvania/, domain: "iu.edu", color: "#990000" },
  { key: "osu", name: "Ohio State", match: /ohio state/, domain: "osu.edu", color: "#BB0000" },
  { key: "purdue", name: "Purdue", match: /purdue/, domain: "purdue.edu", color: "#CFB991" },
  { key: "minnesota", name: "Minnesota", match: /university of minnesota/, domain: "umn.edu", color: "#7A0019" },

  // ── International ───────────────────────────────────────────────────
  { key: "oxford", name: "Oxford", match: /university of oxford|^oxford$/, domain: "ox.ac.uk", color: "#002147" },
  { key: "cambridge", name: "Cambridge", match: /university of cambridge|^cambridge$/, domain: "cam.ac.uk", color: "#A3C1AD" },
  { key: "lse", name: "LSE", match: /london school of economics|^lse$/, domain: "lse.ac.uk", color: "#E10600" },
];

/** The library entry for a free-text school name, or null. */
export function findUniversity(school: string | null | undefined): UniversityLogo | null {
  const s = (school || "").toLowerCase().replace(/\s+/g, " ").trim();
  if (!s) return null;
  return UNIVERSITY_LOGOS.find((u) => u.match.test(s) && !(u.exclude && u.exclude.test(s))) ?? null;
}

/** The favicon service answers unknown domains with a 16px placeholder
 *  globe; anything this small is treated as "no logo". */
export const MIN_LOGO_PX = 24;

/** Image URLs to try for a school's mark, best first. */
export function universityLogoSources(u: UniversityLogo): string[] {
  const out: string[] = [];
  if (u.asset) out.push(u.asset);
  out.push(`https://www.google.com/s2/favicons?domain=${u.domain}&sz=128`);
  return out;
}

/** True when two school names are the same school. */
export function sameUniversity(a: string | null | undefined, b: string | null | undefined): boolean {
  if (!a || !b) return false;
  const ua = findUniversity(a);
  const ub = findUniversity(b);
  if (ua || ub) return !!ua && !!ub && ua.key === ub.key;
  const norm = (x: string) => x.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  return norm(a) === norm(b);
}
