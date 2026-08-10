# Examples

`vault/` is a fully synthetic demo vault. Nothing in it describes real people,
products, or companies. Its registry uses schema v2, so it also demonstrates
the federation card fields. It demonstrates every core concept:

- **Routing**: four registered wikis with routing cards, digests, and indexes.
- **Privacy boundaries**: `ProductWiki` is `public-reference`, `BrandingWiki`
  is `company-private`, `ResearchDigest` is `digest-only` (only its digest is
  ever routed), and `ArchiveBox` is `pointer-only` (only the path is returned).
- **Supersession**: `ProductWiki/topics/pricing-model.md` is `superseded` and
  points at `pricing-v2.md`; the old decision stays for history.
- **Capture**: `.megamind/proposals/` holds one applied proposal (the pricing
  change) and one still-open proposal waiting for categorization.
- **Promotion**: `ProductWiki/topics/integrations/` has four pages and no
  index; `megamind-axi review` flags it as a micro-wiki candidate.

## Try it

```sh
cd examples/vault

megamind-axi route "how does pricing work now"
megamind-axi route "what is our brand color palette"
megamind-axi route "research findings about notifications"   # digest-only
megamind-axi route "old archived history"                     # pointer-only
megamind-axi route "quantum llama farming"                    # explicit no-match

megamind-axi review --today 2026-06-01
megamind-axi doctor

# The same vault as a fleet of one: catalog and model-aware preflight
megamind-axi --root examples/vault catalog --today 2026-06-01
megamind-axi --root examples/vault preflight "what is our brand color palette" --model-class cloud
megamind-axi --root examples/vault preflight "what is our brand color palette" --model-class local

# Capture something new, inspect the proposal, then plan and apply it
megamind-axi capture --text "Exports gain a rate-limit override for admins." --type decision
megamind-axi review --today 2026-06-01
megamind-axi evolve <proposal-id>            # dry-run diff plus plan id
megamind-axi evolve <proposal-id> --apply --plan-id <plan-id>
```

The vault was generated with Megamind itself. Feel free to break it and run
`megamind-axi doctor` to watch the findings appear.
