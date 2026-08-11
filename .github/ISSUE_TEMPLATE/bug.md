---
name: Bug
about: Something does not work the way it says it does
labels: bug
---

## What happened

<!-- The symptom, as you saw it. "The board drew the wrong order" tells us more
     than "sorting is broken". -->

## What you expected

## `ta doctor`

<!-- Paste the whole output. It answers most of the first round of questions:
     which integrations are live, where the config lives, and what language the
     parser is using. -->

```
$ ta doctor

```

## Logs, if the daemon is involved

```
$ journalctl --user -u ta -n 50

```

## Anything else

<!-- Have you checked docs/troubleshooting.md? Not a requirement — but if your
     symptom is in there and the fix did not work, that is very useful to know. -->
