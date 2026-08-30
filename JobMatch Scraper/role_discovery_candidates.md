# Adoption candidates — review before anything is written

From a `--dry-run` of `scraper/adopt_everify_boards.py`. **Nothing has been added to the boards table.** Funnel: 943 probed -> 153 boards (16.2 per cent) -> 139 confirmed identities -> **133 would-add**.


## Recommendation: adopt 132, reject 1

**Reject `Jobgether`** (lever, 4,094 postings, 1,241 passing the title filter - the largest board in the run). It is a remote-jobs **aggregator, not an employer**. Measured directly: 1,730 distinct titles across 4,094 postings; spread over ~30 countries (US 1,046, India 339, Brazil 336, Canada 327, Switzerland 170, UK 169); and **every row scrapes with an empty company field**, so all of them would enter the feed credited to "Jobgether" instead of the real employer. That is the standing no-aggregator rule - the same reasoning that removed Adzuna - and it is a data-quality failure, not a volume one.


## The big low-ratio boards, titles actually read

The zero-yield check only auto-rejects at ZERO survivors, so a large board with a thin ratio still needs eyes. **Concentration beats ratio:**

- **Tapestry** - workday, 1,987 postings, 21 pass (1.1 per cent). **KEEP.** All 21 survivors are *distinct* and genuinely HQ: Infrastructure Platform Engineer, Retail Technology Program Manager, Sr. Microsoft 365 Platform Engineer, Senior Manager Data Engineer, Project Portfolio Manager. Zero repetition. This is the Dollar General pattern (kept), not Family Dollar (blocked) - and a clearer keep than Petco was at 4-of-2,000.
- **Intuitive** - smartrecruiters, 661 postings, 121 pass (18.3 per cent). **KEEP.** 111 distinct titles, all on target.


## Auto-rejected by the pipeline, correctly

- **Domino's** - title filter keeps 0 of 1000
- **AutoZone** - company is on the admin blocklist
- **Mercy** - company is on the admin blocklist
- **Best Version Media** - title filter keeps 0 of 725

## Held back by the identity guard

Slug guessing is wrong more often than it looks. These graded `review`/`guess` and were **not** added:

- **Relativity** -> the board reports "Relativity Space" (match score 76)
- **CoStar Group** -> the board reports "Co–Star" (match score 80)
- **AJ Boggs** -> the board reports "AJ Boggs/ProPower" (match score 64)
- **Trase** -> the board reports "Trase Systems" (match score 55)
- **McAfee** -> the board reports "McAfee Heating and Air Conditioning" (match score 29)
- **SAS** -> the board reports "Superior Alarm Systems" (match score 24)
- **Superior** -> the board reports "Superior Animal Hospital & Boarding Suites" (match score 33)
- **FTS, Inc.** -> the board reports "Flores Technical Services" (match score 21)

## Sponsorship profile of the 132 recommended

- with H-1B filings: **47**
- `stem_opt` tagged: 9
- cap-exempt: 2 - University of Cincinnati, University of Central Florida (no H-1B lottery, the best route)
- no federal record at all: 81 - ranked low, **not** dropped; absence is not evidence of non-sponsorship
- total live postings behind them: **9508**


## The 132 recommended adoptions

| Employer | ATS | Jobs | H-1B | stem_opt | cap-exempt | Confidence | Board |
|---|---|---:|---:|---|---|---|---|
| University of Cincinnati | successfactors | 343 | 306 |  | yes | high | `https://jobs.uc.edu` |
| Sonos, Inc. | workday | 36 | 191 |  |  | high | `https://sonos.wd1.myworkdayjobs.com/Sonos` |
| Medallia | jibe | 63 | 168 |  |  | high | `https://jobs.medallia.com` |
| University of Central Florida | workday | 11 | 163 |  | yes | high | `https://ucf.wd1.myworkdayjobs.com/athletics` |
| ZoomInfo | greenhouse | 111 | 130 |  |  | low | `https://job-boards.greenhouse.io/zoominfo` |
| Tapestry | workday | 1987 | 128 | yes |  | high | `https://tapestry.wd108.myworkdayjobs.com/Tapestry_Careers` |
| Oliver Wyman | lever | 2 | 91 |  |  | low | `https://jobs.lever.co/oliverwyman` |
| Klaviyo | greenhouse | 143 | 68 |  |  | low | `https://job-boards.greenhouse.io/klaviyo` |
| DriveWealth | greenhouse | 18 | 56 |  |  | low | `https://job-boards.greenhouse.io/drivewealth` |
| Tenstorrent | greenhouse | 140 | 19 |  |  | low | `https://job-boards.greenhouse.io/tenstorrent` |
| Otter.ai | greenhouse | 30 | 17 |  |  | low | `https://job-boards.greenhouse.io/otterai` |
| Adyen | greenhouse | 224 | 15 |  |  | low | `https://job-boards.greenhouse.io/adyen` |
| Taxbit | greenhouse | 12 | 14 |  |  | low | `https://job-boards.greenhouse.io/taxbit` |
| Entrata | lever | 14 | 13 |  |  | low | `https://jobs.lever.co/entrata` |
| Haworth | successfactors | 38 | 9 |  |  | high | `https://careers.haworth.com` |
| Plexus Corp. | lever | 2 | 9 |  |  | low | `https://jobs.lever.co/plexus` |
| Exiger | greenhouse | 46 | 8 |  |  | low | `https://job-boards.greenhouse.io/exiger` |
| Veritone | workday | 8 | 8 |  |  | high | `https://veritone.wd1.myworkdayjobs.com/Veritone_Career_Site` |
| Stratasys | successfactors | 53 | 7 |  |  | high | `https://careers.stratasys.com` |
| Twenty | ashby | 28 | 7 |  |  | low | `https://jobs.ashbyhq.com/twenty` |
| Salient | ashby | 12 | 6 | yes |  | low | `https://jobs.ashbyhq.com/salient` |
| Kargo | greenhouse | 16 | 5 |  |  | low | `https://job-boards.greenhouse.io/kargo` |
| Garage | ashby | 16 | 5 | yes |  | low | `https://jobs.ashbyhq.com/garage` |
| Sofar Ocean | ashby | 9 | 5 |  |  | low | `https://jobs.ashbyhq.com/sofarocean` |
| Lincoln International | greenhouse | 42 | 4 |  |  | low | `https://job-boards.greenhouse.io/lincolninternational` |
| Endor Labs | greenhouse | 27 | 4 |  |  | low | `https://job-boards.greenhouse.io/endorlabs` |
| Vorto | ashby | 6 | 4 |  |  | low | `https://jobs.ashbyhq.com/vorto` |
| 24 Hour Fitness | smartrecruiters | 1 | 4 |  |  | low | `https://jobs.smartrecruiters.com/24HourFitness` |
| Acrisure | workday | 231 | 3 |  |  | high | `https://acrisure.wd1.myworkdayjobs.com/Acrisure` |
| BeyondTrust | greenhouse | 66 | 3 |  |  | low | `https://job-boards.greenhouse.io/beyondtrust` |
| Bubble | ashby | 10 | 3 |  |  | low | `https://jobs.ashbyhq.com/bubble` |
| Aptos Labs | greenhouse | 8 | 3 |  |  | low | `https://job-boards.greenhouse.io/aptoslabs` |
| Deepgram | ashby | 93 | 2 | yes |  | low | `https://jobs.ashbyhq.com/deepgram` |
| Garner Health | greenhouse | 81 | 2 |  |  | low | `https://job-boards.greenhouse.io/garnerhealth` |
| Empower Pharmacy | greenhouse | 61 | 2 |  |  | low | `https://job-boards.greenhouse.io/empowerpharmacy` |
| Sesame | ashby | 25 | 2 |  |  | low | `https://jobs.ashbyhq.com/sesame` |
| Arcadia | lever | 17 | 2 |  |  | low | `https://jobs.lever.co/arcadia` |
| Beacon AI | ashby | 15 | 2 |  |  | low | `https://jobs.ashbyhq.com/beaconai` |
| Fieldguide | ashby | 47 | 1 |  |  | low | `https://jobs.ashbyhq.com/fieldguide` |
| Octave | greenhouse | 30 | 1 |  |  | low | `https://job-boards.greenhouse.io/octave` |
| NMI | greenhouse | 24 | 1 |  |  | low | `https://job-boards.greenhouse.io/nmi` |
| Alchemy | ashby | 22 | 1 |  |  | low | `https://jobs.ashbyhq.com/alchemy` |
| Merge | ashby | 17 | 1 |  |  | low | `https://jobs.ashbyhq.com/merge` |
| Campus | ashby | 17 | 1 |  |  | low | `https://jobs.ashbyhq.com/campus` |
| David | greenhouse | 8 | 1 |  |  | low | `https://job-boards.greenhouse.io/david` |
| Agave | ashby | 7 | 1 | yes |  | low | `https://jobs.ashbyhq.com/agave` |
| Quilt | greenhouse | 5 | 1 |  |  | low | `https://job-boards.greenhouse.io/quilt` |
| Intuitive | smartrecruiters | 661 | 0 |  |  | low | `https://jobs.smartrecruiters.com/Intuitive` |
| Air Apps | ashby | 498 | 0 |  |  | low | `https://jobs.ashbyhq.com/airapps` |
| Ardent Health | workday | 461 | 0 |  |  | high | `https://ensemblehp.wd5.myworkdayjobs.com/EnsembleHealthPartnersCareers` |
| Luminis Health | greenhouse | 428 | 0 |  |  | low | `https://job-boards.greenhouse.io/luminishealth` |
| ICE | jibe | 292 | 0 |  |  | high | `https://careers.ice.com` |
| Clera | ashby | 284 | 0 | yes |  | low | `https://jobs.ashbyhq.com/clera` |
| Oldcastle BuildingEnvelope | greenhouse | 192 | 0 |  |  | low | `https://job-boards.greenhouse.io/oldcastlebuildingenvelope` |
| IDEXX | workday | 191 | 0 |  |  | high | `https://idexx.wd115.myworkdayjobs.com/IDEXX` |
| 2K | greenhouse | 117 | 0 |  |  | low | `https://job-boards.greenhouse.io/2k` |
| GTI Fabrication | lever | 103 | 0 |  |  | low | `https://jobs.lever.co/gtifabrication` |
| Nexthink | smartrecruiters | 103 | 0 |  |  | low | `https://jobs.smartrecruiters.com/Nexthink` |
| North Point Technology | greenhouse | 95 | 0 |  |  | low | `https://job-boards.greenhouse.io/northpointtechnology` |
| Atoms | greenhouse | 94 | 0 | yes |  | low | `https://job-boards.greenhouse.io/atoms` |
| Epicor | workday | 86 | 0 |  |  | high | `https://epicorsoftware.wd5.myworkdayjobs.com/epicorjobs` |
| Art of Problem Solving | greenhouse | 84 | 0 |  |  | low | `https://job-boards.greenhouse.io/artofproblemsolving` |
| Hightouch | greenhouse | 80 | 0 |  |  | low | `https://job-boards.greenhouse.io/hightouch` |
| Boccard | smartrecruiters | 79 | 0 |  |  | low | `https://jobs.smartrecruiters.com/Boccard` |
| Polymarket | ashby | 77 | 0 |  |  | low | `https://jobs.ashbyhq.com/polymarket` |
| ClickUp | ashby | 67 | 0 |  |  | low | `https://jobs.ashbyhq.com/clickup` |
| Instawork | greenhouse | 57 | 0 |  |  | low | `https://job-boards.greenhouse.io/instawork` |
| Defense Unicorns | greenhouse | 56 | 0 |  |  | low | `https://job-boards.greenhouse.io/defenseunicorns` |
| Emmes Group | jibe | 49 | 0 |  |  | high | `https://careers.emmes.com` |
| Cloudbeds | greenhouse | 46 | 0 |  |  | low | `https://job-boards.greenhouse.io/cloudbeds` |
| Blitzy | ashby | 46 | 0 |  |  | low | `https://jobs.ashbyhq.com/blitzy` |
| Nortal | greenhouse | 43 | 0 |  |  | low | `https://job-boards.greenhouse.io/nortal` |
| Abridge | ashby | 42 | 0 |  |  | low | `https://jobs.ashbyhq.com/abridge` |
| Trove Brands | greenhouse | 41 | 0 |  |  | low | `https://job-boards.greenhouse.io/trovebrands` |
| Acron Aviation | lever | 40 | 0 |  |  | low | `https://jobs.lever.co/acronaviation` |
| Alteva RCM | greenhouse | 39 | 0 |  |  | low | `https://job-boards.greenhouse.io/altevarcm` |
| Scribe | ashby | 38 | 0 |  |  | low | `https://jobs.ashbyhq.com/scribe` |
| SmithRx | greenhouse | 37 | 0 |  |  | low | `https://job-boards.greenhouse.io/smithrx` |
| Candid Health | ashby | 36 | 0 |  |  | low | `https://jobs.ashbyhq.com/candidhealth` |
| Lab37 | greenhouse | 33 | 0 |  |  | low | `https://job-boards.greenhouse.io/lab37` |
| Innomotics | successfactors | 32 | 0 |  |  | high | `https://jobs.innomotics.com` |
| Phenom | phenom | 32 | 0 |  |  | high | `https://careers.phenom.com` |
| Rillet | ashby | 32 | 0 |  |  | low | `https://jobs.ashbyhq.com/rillet` |
| Bobyard | ashby | 30 | 0 |  |  | low | `https://jobs.ashbyhq.com/bobyard` |
| Edmentum | greenhouse | 30 | 0 |  |  | low | `https://job-boards.greenhouse.io/edmentum` |
| Vendelux | ashby | 29 | 0 |  |  | low | `https://jobs.ashbyhq.com/vendelux` |
| AfterQuery | ashby | 28 | 0 |  |  | low | `https://jobs.ashbyhq.com/afterquery` |
| Everlywell | lever | 22 | 0 |  |  | low | `https://jobs.lever.co/everlywell` |
| Fingerprint | greenhouse | 22 | 0 |  |  | low | `https://job-boards.greenhouse.io/fingerprint` |
| SageSure | greenhouse | 22 | 0 |  |  | low | `https://job-boards.greenhouse.io/sagesure` |
| FareHarbor | greenhouse | 21 | 0 |  |  | low | `https://job-boards.greenhouse.io/fareharbor` |
| Flagler Health | ashby | 20 | 0 |  |  | low | `https://jobs.ashbyhq.com/flaglerhealth` |
| Advanced Space | greenhouse | 18 | 0 |  |  | low | `https://job-boards.greenhouse.io/advancedspace` |
| Chalk | ashby | 17 | 0 |  |  | low | `https://jobs.ashbyhq.com/chalk` |
| Nabla | ashby | 16 | 0 |  |  | low | `https://jobs.ashbyhq.com/nabla` |
| OpenEye | greenhouse | 16 | 0 |  |  | low | `https://job-boards.greenhouse.io/openeye` |
| NetDocuments | greenhouse | 15 | 0 |  |  | low | `https://job-boards.greenhouse.io/netdocuments` |
| RF-SMART | greenhouse | 15 | 0 |  |  | low | `https://job-boards.greenhouse.io/rfsmart` |
| Hone Health | greenhouse | 14 | 0 |  |  | low | `https://job-boards.greenhouse.io/honehealth` |
| Camber | ashby | 14 | 0 |  |  | low | `https://jobs.ashbyhq.com/camber` |
| Luminai | ashby | 14 | 0 |  |  | low | `https://jobs.ashbyhq.com/luminai` |
| Office Ally | greenhouse | 14 | 0 |  |  | low | `https://job-boards.greenhouse.io/officeally` |
| Revivn | greenhouse | 14 | 0 |  |  | low | `https://job-boards.greenhouse.io/revivn` |
| Vast.ai | ashby | 14 | 0 |  |  | low | `https://jobs.ashbyhq.com/vastai` |
| Atticus | ashby | 13 | 0 |  |  | low | `https://jobs.ashbyhq.com/atticus` |
| FacilityOS | ashby | 13 | 0 |  |  | low | `https://jobs.ashbyhq.com/facilityos` |
| Pylon | ashby | 12 | 0 | yes |  | low | `https://jobs.ashbyhq.com/pylon` |
| RVO Health | greenhouse | 12 | 0 |  |  | low | `https://job-boards.greenhouse.io/rvohealth` |
| Coastal | ashby | 12 | 0 |  |  | low | `https://jobs.ashbyhq.com/coastal` |
| Impiricus | greenhouse | 12 | 0 |  |  | low | `https://job-boards.greenhouse.io/impiricus` |
| CompanyCam | greenhouse | 11 | 0 |  |  | low | `https://job-boards.greenhouse.io/companycam` |
| Gradera | ashby | 11 | 0 |  |  | low | `https://jobs.ashbyhq.com/gradera` |
| Honeycomb Insurance | greenhouse | 11 | 0 |  |  | low | `https://job-boards.greenhouse.io/honeycombinsurance` |
| Technology Navigators | smartrecruiters | 10 | 0 |  |  | low | `https://jobs.smartrecruiters.com/TechnologyNavigators` |
| Authorium | ashby | 8 | 0 |  |  | low | `https://jobs.ashbyhq.com/authorium` |
| Pryzm | ashby | 7 | 0 |  |  | low | `https://jobs.ashbyhq.com/pryzm` |
| Unwrap | ashby | 7 | 0 |  |  | low | `https://jobs.ashbyhq.com/unwrap` |
| TALENT Software Services | smartrecruiters | 6 | 0 |  |  | low | `https://jobs.smartrecruiters.com/TALENTSoftwareServices` |
| Zus Health | lever | 6 | 0 |  |  | low | `https://jobs.lever.co/zushealth` |
| Warp | greenhouse | 5 | 0 |  |  | low | `https://job-boards.greenhouse.io/warp` |
| You.com | greenhouse | 5 | 0 |  |  | low | `https://job-boards.greenhouse.io/youcom` |
| Horizontal Talent | smartrecruiters | 4 | 0 |  |  | low | `https://jobs.smartrecruiters.com/HorizontalTalent` |
| Accord | ashby | 4 | 0 |  |  | low | `https://jobs.ashbyhq.com/accord` |
| Brilliant® | lever | 4 | 0 |  |  | low | `https://jobs.lever.co/brilliant` |
| OneCrew | ashby | 4 | 0 |  |  | low | `https://jobs.ashbyhq.com/onecrew` |
| Bestgate Engineering | smartrecruiters | 3 | 0 |  |  | low | `https://jobs.smartrecruiters.com/BestgateEngineering` |
| TechTree | smartrecruiters | 3 | 0 |  |  | low | `https://jobs.smartrecruiters.com/TechTree` |
| Kasheesh | lever | 2 | 0 |  |  | low | `https://jobs.lever.co/kasheesh` |
| Urban Sky | greenhouse | 2 | 0 |  |  | low | `https://job-boards.greenhouse.io/urbansky` |
| Emergence AI | greenhouse | 1 | 0 | yes |  | low | `https://job-boards.greenhouse.io/emergenceai` |
| Bridge Atlantic | smartrecruiters | 1 | 0 |  |  | low | `https://jobs.smartrecruiters.com/BridgeAtlantic` |
| Visionary Innovative Technology Solutions LLC | smartrecruiters | 1 | 0 |  |  | low | `https://jobs.smartrecruiters.com/VisionaryInnovativeTechnologySolutionsLLC` |
