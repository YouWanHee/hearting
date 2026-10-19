// OpenCode 2 discovers local server plugins by package directory and imports
// its index entrypoint. Keep the V1 file as the compatibility server export;
// the V2 package surface uses only the Promise `id/setup` definition.
import Plugin from "../hearting-guards.js"

export default { id: Plugin.id, setup: Plugin.setup }
