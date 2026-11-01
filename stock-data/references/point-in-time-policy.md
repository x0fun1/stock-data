# Point-in-Time Policy (v4)

- The news window is `(as-of − 60 days, as-of]`. Records without a timezone-aware publication timestamp, or with `published_at > as-of`, are excluded and counted.
- The collector can query historical dates, but publication time does not prove the item was visible then. Historical output therefore carries `pit_status=published_at_only` and must not claim a reconstructed information set.
- Market data is a download-time revised series, not a historical vintage. Descriptive price reactions use only sessions already present in the frozen snapshot; no forward session is inferred.
- Verification uses live Search/Fetch when as-of is the run date (`verification_pit=live`). An earlier as-of is `post_hoc`; those items cannot be rated strong.
- Updated timestamps are not publication timestamps. A record updated after the as-of is excluded.
- The runtime keeps no local news archive and does not fall back to a previous fetch. A failed source is `error`, not stale data.
