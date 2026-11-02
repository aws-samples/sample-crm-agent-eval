// SPDX-License-Identifier: MIT
/**
 * Dev-server entry point, injected into `index.html` by `devServerPlugin` in `serve` mode only.
 *
 * Seeds a dev session before the application bundle runs, so `CognitoAuthSession.restore()` finds
 * one and the router reaches real pages. Choose the rep with `?devRep=scoped|manager|empty`, or clear the
 * session with `?devRep=none` to review the signed-out surface.
 *
 * Never imported by application code.
 */

import { DEV_REPS, clearDevCognitoSession, seedDevCognitoSession } from "./devToken";

const DEV_CLIENT_ID = "dev-client-id";

function selectedRep(): keyof typeof DEV_REPS | "none" {
  const requested = new URLSearchParams(window.location.search).get("devRep");
  if (requested === "none" || requested === "manager" || requested === "scoped" || requested === "empty")
    return requested;
  return "scoped";
}

const choice = selectedRep();
if (choice === "none") {
  for (const rep of Object.values(DEV_REPS)) clearDevCognitoSession(DEV_CLIENT_ID, rep);
} else {
  seedDevCognitoSession(DEV_CLIENT_ID, DEV_REPS[choice]);
}

console.info(
  `[dev api stub] signed in as ${choice === "none" ? "nobody" : DEV_REPS[choice].repId}. ` +
    "Switch with ?devRep=scoped|manager|empty|none. This stub is dev-only and is absent from a build.",
);
