-- 计划升级窗口的准入判断：只有显式、会过期的部署窗口才进等待代理。
--
-- 两条必须绕过的路径（放在本文件里而不是指望 location 优先级，因为 access_by_lua_file
-- 一旦被提到 server 级就会继承到精确 location）：
--   * /__waiting/  —— 等待页与 reconnect.js 由 nginx 本地静态目录供应，
--                     进等待代理就永远拿不到自己的脚本；
--   * /api/app/ready —— 冷打开/已打开页面的轻量探针，升级闸门会立刻回
--                     {"ready": false}；排进 180 秒队列等于让探针自己等自己。
local uri = ngx.var.uri or ''
if uri:sub(1, 11) == '/__waiting/' or uri == '/api/app/ready' then
    return
end

local f = io.open('/run/wb2api-upgrade/active', 'r')
if f then
    local expires = tonumber(f:read('*l')) or 0
    f:close()
    if expires > ngx.now() then
        return ngx.exec('@wb2api_upgrade_wait')
    end
end
