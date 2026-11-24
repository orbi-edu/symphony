# AGENTS.md

Always-active conventions and lessons (curated via symphony-learnings).

## 4. Rules that you must obey
- Write all code, comments, documentation, commit messages, log events and user-facing text in
- A ticket that sent you here (an investigation of a failed run, say) is private; describe the  <!-- source: n/a -->

## 5. Commits and pull requests
- One commit has one purpose. Write the subject in the imperative mood, 72 characters or less,
- A pull request links its issue, says how you verified the change, and lists the risks.  <!-- source: n/a -->

## 6. When you are not sure
- Read the code and the documents first. Most answers are there.
- If a requirement is not clear, stop and ask. Do not guess about credentials, data loss or  <!-- source: n/a -->

## Match pod eviction-protection annotations to the cluster's autoscaler
- Karpenter (including EKS Auto Mode) ignores cluster-autoscaler.kubernetes.io/safe-to-evict. Only karpenter.sh/do-not-disrupt keeps a pod from being disrupted.
- Pitfall: when protecting long-running pods (runners, busy workspaces) from node consolidation or scale-down, it is easy to add only `cluster-autoscaler.kubernetes.io/safe-to-evict: "false"`. On Karpenter-managed nodes, including EKS Auto Mode, Karpenter ignores that annotation, so the pods can still be disrupted and the run can die partway through. In this issue, attempt 1 failed that way. Next action: before writing eviction or disruption annotations into a pod template, find out which autoscaler manages the target nodes (look for Karpenter NodePools/NodeClaims or the cluster-autoscaler deployment). If it is unknown or could be either, set both `cluster-autoscaler.kubernetes.io/safe-to-evict: "false"` and `karpenter.sh/do-not-disrupt: "true"` on the pod template, not on the Job object. Say in the docs which autoscalers each annotation covers.  <!-- source: ORB-660 -->
