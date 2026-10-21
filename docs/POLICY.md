# Policy — what Damien believes, and why

Damien's behaviour is not one prompt. It is three layers, and the order between them is
the whole design.

## The three layers, in order of authority

| layer | who sets it | wins over | cost |
|---|---|---|---|
| **corrections** (`damien correct`, `restore`) | you, one message at a time | everything | free |
| **rules** (`~/.damien/rules.json`) | you, `rules from-senders`, `rules from-group` | the model | free |
| **buckets** (`DEFAULT_SPEC` in `src/damien/decide.py`) | the code | nothing | ≈$0.000046 a message |

A correction is recorded on the message, so the next plan agrees with you without a rule.
A rule is *deterministic*: the same message always lands the same way, with no model call
and no charge. The buckets are the model's choices, and they are the only layer that can
be wrong in a way nobody wrote down — which is why they are last and why every verdict is
cached and stored with its rationale.

## The buckets

The model is not asked "what should I do"; it is asked to pick the bucket that fits, one
question per message, and the bucket carries the action.

| bucket | what it means | action |
|---|---|---|
| `keep` | belongs in the inbox: a person wrote it, it needs attention, or it is fashion, music-industry, AI or technology mail he follows | keep |
| `receipt` | an automated receipt, order or confirmation for something already done | label **Receipts** |
| `newsletter` | a newsletter he subscribed to, not about fashion, music, AI or technology | label **Newsletters** |
| `promotion` | a discount, sale, pitch or upsell — **including marketing from a company he already uses**, and never news about that company's field | trash |
| `notification` | automated social, app, security or shipping chatter with nothing to do | archive |
| `archive` | once-useful, now done: keep the message, get it out of the way | archive |
| `unclear` | cannot be told from the headers and snippet | keep |

`promotion` is the bucket the policy has been tuned against most, and its wording is
deliberate: a *pitch from AWS about a product he already uses* is marketing, not AWS
news. That distinction is the difference between a hundred messages of noise in the inbox
and a hundred out of it.

## The rule vocabulary

A rule is a list of conditions (AND), optional OR-groups, and an action:

```json
{
  "id": "sell-amazon",
  "name": "Amazon seller mail -> Promotions tab",
  "enabled": true,
  "match": [{ "field": "from", "op": "contains", "value": "sell.amazon.com" }],
  "any_of": [],
  "action": "label",
  "label": "CATEGORY_PROMOTIONS",
  "keep_in_inbox": true,
  "source": "person",
  "note": "He is an Amazon seller, so this sender mixes marketing with compliance..."
}
```

- **Fields**: `from`, `to`, `subject`, `snippet`, `any`, `label`, `age_days`,
  `attachment`, `list_unsubscribe`, `list_id`.
- **Operators**: `contains`, `not_contains`, `equals`, `not_equals`, `starts_with`,
  `ends_with`, `regex`, `words`, `gt`, `lt`, `exists`, `not_exists`.
- **Actions**: `keep`, `archive`, `trash`, `label`, `mark_read`.
- **`keep_in_inbox`** (with `label`): apply the label and *leave the message in the
  inbox* — this is what "put it in the Promotions tab" means, because the tab is a view
  of the inbox. Without it, labelling removes INBOX and the message goes to All Mail.
- **`source`**: `person` (you wrote it), `seed` (shipped), `jev` (minted from a plan).
  A person rule always beats a seeded one on the same message.

**What Gmail can evaluate itself**: a rule is translated to a Gmail query when its
conditions are translatable (`from:`, `subject:`, `label:`, `older_than:` …), which is
what makes `rules plan` free and fast. A rule using a regex or a snippet match cannot be
translated and is evaluated in Python over fetched headers — correct, but it costs a
fetch, so `rules list` marks which is which.

## The policy record — the rules that encode a decision

Every rule carries a `note` saying *why*. That note is the policy; it is what makes this
file reviewable. The decisions that shaped the mailbox:

| decision | rule(s) | why |
|---|---|---|
| **the category shortcut is a trap** | — (removed) | A rule keyed on a Gmail category label looked exact, but labels are applied by Gmail to things the sender never confirmed; it filed mail nobody had looked at. Removed in favour of the sender. |
| **a sender is a class, intent is about content** | `ali-*`, `li-jobalerts`, `ind-jobalerts` | Alibaba sends seven kinds of mail from seven addresses: inquiries and seller messages are transactional, sourcing picks and low-MOQ pitches are marketing. The sender level is where the split is visible — so each address got a rule. |
| **vendor marketing is still marketing** | spec (`promotion`) | A product pitch from AWS, GitHub, Amazon or Shopify is not news about that company's field. Same shape as Alibaba, at the model layer instead of the sender layer. |
| **the Promotions tab, not the Trash, when the sender matters** | `sell-amazon` | He *is* an Amazon seller: the same sender carries "889 Federal Compliance Certification" and "🚀 Amazon Business Hit $60B in Sales". Trashing that sender wholesale would eventually lose a compliance notice. Marked Promotions, kept in the inbox, and `r-old-promotions` still trashes the year-old ones. |
| **a service you do not use is marketing** | `uber-eats` | "I don't use that service at all." |
| **a service you do use still sends marketing** | `uber-promos` | His Uber *trips* come from `noreply@uber.com` and are receipts; the 60%-off blasts come from `uber@uber.com`. Two addresses, two answers. |
| **job alerts are promotional whoever is hiring** | `li-jobalerts`, `ind-jobalerts` | Not about him being hired; a feed of other people's postings. |
| **the bank alerts are not noise** | — (left `keep`, on purpose) | 293 accumulated balance alerts are routine — except that the account is in SOS repair, which is exactly when they matter. Kept deliberately. |

## The two lessons that generalise

1. **A sender is a class; intent lives in the content.** Whenever a decision felt wrong,
   it was because two different things arrived from one address — and whenever the fix
   held, it was because the class was named (an address, a subject shape, a category)
   rather than the instance.
2. **A broader rule can silently kill a narrower one.** A rule matching a whole domain
   will swallow a rule matching one address, and the shadowed rule stops firing with no
   error anywhere. `damien rules list` reports shadowed rules; a rule file that adds one
   should be checked for it.

## Changing policy

- **One message**: `damien correct <id> --action archive --note "…"` or `damien restore <id>`.
- **A class you just reviewed**: `damien rules from-group <plan_id> -g <n> --yes` (try `--dry-run`).
- **Every consistent sender in a plan**: `damien rules from-senders <plan_id> --yes`.
- **By hand**: edit `~/.damien/rules.json`; `damien rules list` shows what each rule can do.
- **The buckets**: edit `DEFAULT_SPEC` in `src/damien/decide.py`. This invalidates the
  verdict cache — **every message is re-judged**, which on this mailbox costs about
  $0.17. Change it deliberately, never as part of a scheduled run.
