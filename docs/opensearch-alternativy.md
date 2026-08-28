# Alternativy k OpenSearch pro fulltext-poc

Research z 2026-08-27 (podnět: https://sliplane.io/blog/5-awesome-elasticsearch-alternatives).
Cíl: najít lightweight náhradu za OpenSearch (dnes `-Xms2g -Xmx2g`, LXC host s ~6GB
RAM celkem). Klíčové požadavky, na kterých se lámalo hodnocení:

1. **Česká lemmatizace přes hunspell**, ne jen suffix stemming — viz `Assert A`
   v `make hello` (`smlouvám` musí najít `smlouva`). Hunspell = knihovna pro
   kontrolu pravopisu/morfologii (LibreOffice, Firefox...), pracuje se dvěma
   soubory: `.dic` (slovník lemmat) + `.aff` (odvozovací pravidla pro
   skloňování/časování). Na rozdíl od obyčejného stemmeru skutečně zná slovní
   zásobu, takže zvládá bohatou českou flexi (7 pádů, deklinační vzory,
   nepravidelnosti) — mechanické ořezávání koncovek na to nestačí. Repo dnes
   používá `opensearch/hunspell/cs_CZ/{cs_CZ.aff,cs_CZ.dic}` stažené
   `scripts/fetch-hunspell.sh` z `github.com/LibreOffice/dictionaries`.
2. Nízká RAM náročnost, ideálně žádná JVM.
3. Phrase queries, BM25-ish relevance, highlighting (`<em>` snippety),
   HTTP/JSON API.
4. Index je disponibilní/rebuildovatelný (`make reindex`), takže migrace není
   požadavek.
5. Musí rozumně běžet pod Podmanem (Quadlets, rootless).

## Srovnání

| Kandidát | Čeština (lemmatizace) | RAM | Highlighting | JVM | Stav |
|---|---|---|---|---|---|
| **PostgreSQL FTS** | lze napojit stejné `cs_CZ.aff/.dic` soubory (custom TS config), ale není to out-of-box | žádná navíc – Postgres už běží ve stacku | `ts_headline()`, konfigurovatelné tagy | ne | součást core Postgresu |
| **Manticore Search** | jen Snowball suffix stemmer (`stem_cz`), **žádný hunspell/lemmatizace** | nejlehčí ze všech kandidátů – cca 40MB idle, běží na 1GB | ano, `HIGHLIGHT()` | ne | aktivní |
| **Apache Solr** | ano, nativně (`HunspellStemFilterFactory` + stejné `.dic/.aff`) | těžký – doporučení 4–6GB heap i pro "malý" index | ano, zralé | ano | aktivní, ale stejná váhová kategorie jako OpenSearch |
| **Typesense** | Snowball přes `locale=cs`, ale nespolehlivé (GitHub issues #1665, #2093) | drží celý index v RAM jako OpenSearch | ano | ne | aktivní |
| **Meilisearch** | žádná potvrzená česká podpora, jen obecné "funguje out of box" tvrzení | dobrá, ~30 % méně RAM než Typesense v jednom benchmarku | ano, konfigurovatelné `<em>` tagy | ne | aktivní |
| **Quickwit** | žádná jazyková podpora tohoto typu (log/observability search, ne app search) | nízká, ale špatný tvar pro tento use case | snippet fields | ne | aktivní, ale nevhodný |
| **ZincSearch** | — | — | — | ne | **archivováno 2026-08-18**, nástupce OpenObserve se přesunul na logy — slepá ulička |
| **Vespa** | nezkoumáno dál | min. 4GB jen na container | — | částečně | vyřazeno – těžší než to, co má nahradit |

## Doporučení: PostgreSQL full-text search

Jediný kandidát, který zároveň zachová skutečnou hunspell lemmatizaci
(recyklací stejných `cs_CZ.aff/.dic` souborů) a nepřidává žádnou novou
službu — Postgres už v stacku běží jako autoritativní metadata store.
`unaccent` nahradí dnešní `asciifolding`, `pg_trgm` pokryje fuzzy/typo
tolerance, `ts_headline()` dá highlighting ekvivalentní dnešnímu `<em>`.

**Rizika/mezery:**

- **Není turnkey.** Čeština není mezi defaultními Postgres TS configs (jen
  Dánština, Nizozemština, Angličtina, Finština, Francouzština, Němčina,
  Maďarština, Italština, Norština, Portugalština, Rumunština, Ruština,
  Španělština, Švédština, +simple). Custom `czech` config je třeba postavit
  ručně z vendorovaných dictionary souborů. Jediný nalezený referenční
  projekt (`KeenMate/hunspell-cz-sk`) je tenký (1 star) — brát jako
  inspiraci, ne jako hotovou závislost.
- **Reálná integrační práce.** `search-api` dnes mluví REST/JSON s OpenSearch
  přes `opensearch-py` — přechod na Postgres = přepsat dotazovou vrstvu na
  SQL (`to_tsvector`/`to_tsquery`/`ts_rank`/`ts_headline`) a
  `make bootstrap`/`make reindex` postavit kolem GIN indexů místo index
  template.
- **Relevance scoring je hrubší** než BM25 — `ts_rank`/`ts_rank_cd` je pro
  korpus téhle velikosti použitelné, ale je to krok zpět oproti Lucene
  rodině.
- Než se do toho investuje čas, stálo by za to prototypovat proti vlastnímu
  `Assert A` z `make hello` (`smlouvám`→`smlouva`), protože kvalita
  lemmatizace v custom TS configu je zatím neověřená.

## Runners-up

- **Manticore Search** — nejlehčí opravdový search engine, REST/JSON API
  nejblíž drop-in náhradě za OpenSearch, ale česká podpora je jen suffix
  stemming, ne hunspell — přesně ten downgrade, který projekt už jednou
  vědomě odmítl (viz README "Czech analysis" sekce). Stálo by za zvážení
  jedině pokud by `stem_cz` empiricky prošel lemmatizačními asserty.
- **Apache Solr** — jediný engine s prvotřídní, doslovnou hunspell podporou
  pro `cs_CZ`, ale je ve stejné JVM/Lucene váhové kategorii jako OpenSearch
  (4–6GB heap doporučení) — lateral move, ne lightweight výhra.
- Zbytek ze zmíněného sliplane.io blogu (Meilisearch, Typesense, Quickwit,
  Zinc, Vespa) na české lemmatizaci nebo architektuře padá rovnou a nevyplatí
  se dál zkoumat pro tento korpus — ten blog cílí na obecný "app search" use
  case, kde na lemmatizaci nezáleží.

## OpenSearch vs. PG FTS — kdy který

Na kvalitu samotné lemmatizace by v zásadě nemělo záležet, které se zvolí —
obě konzumují stejná hunspell data (`cs_CZ.aff/.dic`). Skutečný rozdíl:

- **OpenSearch je ověřená cesta.** `make hello`'s Assert A dokazuje, že
  tahle konkrétní kombinace (Lucene analyzer chain: tokenize → lowercase →
  hunspell stem → asciifolding) funguje na reálném dokumentu. PG cesta je
  neověřená — Postgresův vlastní parser tokenizuje jinak (spojovníky,
  zkratky "č.", "odst.", čísla smluv) a jediný nalezený referenční projekt
  pro tuhle kombinaci je tenký. Nutno empiricky ověřit proti stejnému
  Assert A, než tomu věřit.

**OpenSearch je lepší, když:** korpus poroste za desetitisíce/statisíce
dokumentů a bude třeba horizontální škálování; potřeba bohatší relevance
tuning (field boosting, function score, "more like this"); potřeba
fasetované vyhledávání/agregace (PG by to řešil ručním SQL GROUP BY nad
tsvector); do budoucna sémantické/vektorové hledání (OpenSearch má k-NN
plugin nativně, PG by potřeboval `pgvector` navíc).

**PG FTS je lepší, když:** korpus zůstane v řádu tisíců až nižších
statisíců dokumentů (GIN index na tsvector je pro tuhle velikost rychlý);
záleží na transakční konzistenci (index update jako součást stejné DB
transakce jako metadata, žádný dual-write mezi dvěma systémy — stejný
princip, který `ingest-api` už dnes drží pro S3 write + `status='pending'`
řádek); chce se minimum provozních komponent (žádná druhá služba k
zálohování/monitoringu/JVM tuningu/security pluginu); nepotřebuje se
fasetovaná analytika nad výsledky.

Pro tenhle projekt (PoC smluvního archivu, disponibilní index, cíl
"lightweight"): pokud korpus reálně zůstane v řádu tisíců dokumentů, PG FTS
je architektonicky čistší volba — ale je to sázka, kterou je nutné nejdřív
ověřit proti Assert A, ne rozhodnutí jen na papíře.

## Implementováno (2026-08-28, větev `postgres-fts`)

Assert A ověřen empiricky přímo na cílovém hostiteli (`fn-pg`) ještě před
psaním aplikačního kódu: `to_tsvector('czech_hunspell', 'smlouvám')` přes
Postgresův `ispell` dictionary template lemmatizuje na `smlouva`, stejně
jako předtím OpenSearchův hunspell filtr. Na základě toho byl OpenSearch na
téhle větvi kompletně nahrazen - viz `fulltext-poc/postgres/002-fts.sql`
(schéma), `fulltext-poc/VERSIONS.md` (souhrn rozhodnutí a co bylo ověřeno)
a `fulltext-poc/README.md` (aktuální popis). Větev `main` zůstává na
původním OpenSearch buildu.
