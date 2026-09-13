// Enable separately after reviewing the collector allowlist and privacy notice.
export default Object.freeze({
  enabled: false,
  app: "project_snow",
  endpoint: "https://stats.xiaob.dev/analytics/v1/events",
  paths: ["/"],
  // Public selector roster: backend/snow_app/mvp_character_registry.json
  characters: ["1b0a6b35719a", "25b23cb64398", "41b7444e39cc", "4370a74d6fda", "43f05917bfa1", "447ed3c401c9", "5157b8972632", "6455a5dcff6a", "673ba6851b05", "6862c43d2ac9", "702f4375675b", "78aa7ab99154", "85b205f6f623", "8d5b5c3912bb", "921f9ef0cc4e", "98322bd505f4", "9f5804761c56", "a2ffc5b44d7f", "ca0144ccd81b", "cf0569ac6de9", "d5ecfceba959", "daab0f4cceb4"],
});
