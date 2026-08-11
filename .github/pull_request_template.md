<!--
The commit messages in this repository are written as
problem → decision → what we found on the way. If yours already are, most of
this template is redundant and you can delete it. See CONTRIBUTING.md.
-->

## What was wrong

<!-- The symptom, and how it showed up. This matters more than the code. -->

## What you decided

<!-- And what you rejected, if there was a real choice. -->

## Found on the way

<!-- The thing your plan did not predict. Half the value of this repository's
     history lives in this section — please do not skip it. -->

---

- [ ] `make lint` and `make test` are green
- [ ] Documentation went to its **owner** (see the table in the README), not to
      whichever file was open
- [ ] If the UI changed: **you looked at the screen.** "Returns 200 and contains
      the string" is not interface verification — five bugs in this project's
      history were visible only in a screenshot
- [ ] An ADR, if the decision is hard to reverse, surprising without context, and
      the result of a genuine trade-off. If any of the three is missing, skip it
