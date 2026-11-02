// SPDX-License-Identifier: MIT
// CloudFront viewer-request function for the default (S3) behaviour: serves
// `/index.html` for any client-side route so a deep link or a refresh reaches
// the SPA. The private origin bucket answers a missing key with 403, so a route
// path would otherwise render CloudFront's error document. Attached to the
// default behaviour only; `/api/*` and `/agent/*` responses are unaffected.
//
// A URI whose last path segment contains a `.` is treated as a file and left
// unchanged. Any other URI is rewritten to `/index.html`; method, headers,
// query string and body are untouched.
function handler(event) {
  var request = event.request;
  var uri = request.uri;
  var lastSegment = uri.substring(uri.lastIndexOf('/') + 1);
  if (lastSegment.indexOf('.') === -1) {
    request.uri = '/index.html';
  }
  return request;
}
