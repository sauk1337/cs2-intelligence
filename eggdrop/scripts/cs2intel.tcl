# =============================================================================
#  (Pure ASCII file: texts use \uXXXX escapes so that `source` does not
#   depend on the system encoding.)
#  CS2 Intelligence - Eggdrop script (Tcl 8.6, Eggdrop 1.9+/1.10)
#
#  Bridge between IRC and the Python backend (local HTTP API). Every request
#  is ASYNCHRONOUS (http::geturl -command), so Eggdrop never blocks even if
#  the backend is slow or down.
#
#  Load it from eggdrop.conf:
#     source scripts/cs2intel.conf.tcl   ;# settings (token, URL, network...)
#     source scripts/cs2intel.tcl
#  Enable the bot per channel:   .chanset #cs2 +cs2intel
# =============================================================================

package require Tcl 8.6
package require http 2.7

namespace eval ::cs2intel {
    variable version "1.0.0"
    # defaults (overridden in cs2intel.conf.tcl)
    variable defaults [dict create \
        api_url        "http://127.0.0.1:8787" \
        token          "" \
        network        "minet" \
        admin_flags    "n|n" \
        require_flag   1 \
        allow_private  1 \
        poll_interval  3 \
        poll_limit     6 \
        max_queue      15 \
        max_reply      8 \
        timeout_ms     20000 \
        user_cooldown  2 \
        heartbeat_s    60 \
        user_agent     "CS2Intel-Eggdrop/1.0" \
    ]
    variable commands {
        cs2 help status uptime version about more
        news
        market marketstats cases stickers collections
        skin skinprice skintrend skincompare skinscore skinwatch watch unwatch watchlist
        matches results tournaments major ranking teams team player
        live score match matchstats upcoming finished followmatch unfollowmatch livealerts
        alerts admin
    }
    variable last_cmd       ;# nick -> timestamp (local anti-flood)
    array set last_cmd {}
    variable polling 0
    variable backend_ok 1
    variable fail_count 0
    variable stats
    array set stats {requests 0 errors 0 delivered 0 polls 0}
}

proc ::cs2intel::cfg {key} {
    variable defaults
    if {[info exists ::cs2intel(${key})]} { return $::cs2intel(${key}) }
    return [dict get $defaults $key]
}

# ------------------------------------------------------------------ helpers
proc ::cs2intel::log {msg} { putlog "cs2intel: $msg" }

proc ::cs2intel::clean {text} {
    # strip CR/LF and control chars except colour/bold (prevents IRC command injection)
    set text [string map [list "\r" " " "\n" " " "\x00" ""] $text]
    return [regsub -all {[\x01\x04-\x0e\x10-\x1c\x1e\x1f]} $text ""]
}

proc ::cs2intel::headers {} {
    return [list X-Bot-Token [cfg token]]
}

proc ::cs2intel::channel_enabled {chan} {
    if {![cfg require_flag]} { return 1 }
    if {![validchan $chan]} { return 0 }
    return [channel get $chan cs2intel]
}

proc ::cs2intel::throttled {nick} {
    variable last_cmd
    set now [clock seconds]
    set key [string tolower $nick]
    if {[info exists last_cmd($key)] && ($now - $last_cmd($key)) < [cfg user_cooldown]} { return 1 }
    set last_cmd($key) $now
    # occasional cleanup
    if {[array size last_cmd] > 500} {
        foreach k [array names last_cmd] { if {($now - $last_cmd($k)) > 300} { unset last_cmd($k) } }
    }
    return 0
}

# ------------------------------------------------------------------- commands
proc ::cs2intel::pub {cmd nick uhost hand chan text} {
    if {![channel_enabled $chan]} { return 0 }
    if {[throttled $nick]} { return 0 }
    set admin [expr {$hand ne "*" && [matchattr $hand [cfg admin_flags] $chan] ? 1 : 0}]
    request $cmd $nick $hand $chan $text $admin
    return 1
}

proc ::cs2intel::msg {cmd nick uhost hand text} {
    if {![cfg allow_private]} { return 0 }
    if {[throttled $nick]} { return 0 }
    set admin [expr {$hand ne "*" && [matchattr $hand [cfg admin_flags]] ? 1 : 0}]
    request $cmd $nick $hand "" $text $admin
    return 1
}

proc ::cs2intel::request {cmd nick hand chan text admin} {
    variable stats
    incr stats(requests)
    set query [http::formatQuery network [cfg network] channel $chan nick $nick handle $hand \
                   admin $admin command $cmd args [clean $text]]
    set url "[cfg api_url]/api/v1/command"
    if {[catch {
        http::geturl $url -query $query -headers [headers] -timeout [cfg timeout_ms] \
            -type "application/x-www-form-urlencoded; charset=utf-8" \
            -command [list ::cs2intel::on_reply $nick $chan $cmd]
    } err]} {
        backend_failed "request !$cmd: $err"
        reply_error $nick $chan
    }
}

proc ::cs2intel::reply_error {nick chan} {
    set target [expr {$chan ne "" ? $chan : $nick}]
    puthelp "NOTICE $nick :The CS2 analysis service is not available right now. Please try again in a few minutes."
}

proc ::cs2intel::on_reply {nick chan cmd token} {
    variable stats
    set status [http::status $token]
    set code [expr {$status eq "ok" ? [http::ncode $token] : 0}]
    set data [http::data $token]
    http::cleanup $token
    if {$status ne "ok" || $code != 200} {
        backend_failed "reply !$cmd: status=$status http=$code"
        reply_error $nick $chan
        return
    }
    backend_recovered
    set lines [split $data "\n"]
    if {[lindex $lines 0] ne "OK"} {
        log "unexpected backend reply for !$cmd"
        return
    }
    set target [expr {$chan ne "" ? $chan : $nick}]
    set n 0
    foreach line [lrange $lines 1 end] {
        if {$line eq ""} { continue }
        if {[incr n] > [cfg max_reply]} { break }
        set kind [string index $line 0]
        set text [clean [string range $line 2 end]]
        if {$kind eq "N"} {
            puthelp "NOTICE $nick :$text"
        } else {
            puthelp "PRIVMSG $target :$text"
        }
    }
}

# ------------------------------------------------------------------- outbox
proc ::cs2intel::poll {} {
    variable polling
    variable stats
    # ALWAYS reschedule first: the loop survives any error
    utimer [cfg poll_interval] ::cs2intel::poll
    if {$polling} { return }
    if {![info exists ::server] || $::server eq ""} { return }   ;# not connected to IRC: don't collect
    if {[queuesize help] > [cfg max_queue]} { return }           ;# Eggdrop queue full: wait
    set polling 1
    incr stats(polls)
    set url "[cfg api_url]/api/v1/outbox?[http::formatQuery network [cfg network] limit [cfg poll_limit]]"
    if {[catch {
        http::geturl $url -headers [headers] -timeout [cfg timeout_ms] -command ::cs2intel::on_poll
    } err]} {
        set polling 0
        backend_failed "outbox: $err"
    }
}

proc ::cs2intel::on_poll {token} {
    variable polling
    variable stats
    set polling 0
    set status [http::status $token]
    set code [expr {$status eq "ok" ? [http::ncode $token] : 0}]
    set data [http::data $token]
    http::cleanup $token
    if {$status ne "ok" || $code != 200} {
        backend_failed "outbox: status=$status http=$code"
        return
    }
    backend_recovered
    foreach line [split $data "\n"] {
        if {$line eq ""} { continue }
        set parts [split $line "\t"]
        if {[llength $parts] < 4} { continue }
        lassign $parts id kind target
        set text [clean [join [lrange $parts 3 end] "\t"]]
        set target [clean $target]
        if {[string index $target 0] in {"#" "&" "!" "+"}} {
            if {![validchan $target] || ![botonchan $target]} { continue }
        }
        if {$kind ne "NOTICE"} { set kind "PRIVMSG" }
        puthelp "$kind $target :$text"
        incr stats(delivered)
    }
}

# --------------------------------------------------------------- heartbeat
proc ::cs2intel::heartbeat {} {
    utimer [cfg heartbeat_s] ::cs2intel::heartbeat
    set srv [expr {[info exists ::server] ? $::server : ""}]
    set query [http::formatQuery network [cfg network] botnick $::botnick server $srv channels [join [channels] " "]]
    catch {
        http::geturl "[cfg api_url]/api/v1/heartbeat" -query $query -headers [headers] -timeout 10000 \
            -command [list ::cs2intel::on_heartbeat]
    }
}

proc ::cs2intel::on_heartbeat {token} {
    http::cleanup $token
}

# ------------------------------------------------------------ backend state
proc ::cs2intel::backend_failed {why} {
    variable backend_ok
    variable fail_count
    variable stats
    incr stats(errors)
    incr fail_count
    if {$backend_ok && $fail_count >= 3} {
        set backend_ok 0
        log "backend NOT available ($why). Will keep retrying."
    }
}

proc ::cs2intel::backend_recovered {} {
    variable backend_ok
    variable fail_count
    set fail_count 0
    if {!$backend_ok} {
        set backend_ok 1
        log "backend recovered"
    }
}

# --------------------------------------------------------------- DCC/partyline
proc ::cs2intel::dcc_status {hand idx text} {
    variable stats
    variable backend_ok
    putdcc $idx "cs2intel v$::cs2intel::version \u00b7 network [cfg network] \u00b7 backend [expr {$backend_ok ? "OK" : "DOWN"}] \u00b7 \
requests $stats(requests) \u00b7 errors $stats(errors) \u00b7 polls $stats(polls) \u00b7 delivered $stats(delivered)"
    return 1
}

# ------------------------------------------------------------------ startup
proc ::cs2intel::kill_timers {} {
    foreach t [utimers] {
        if {[string match "*::cs2intel::*" [lindex $t 1]]} { killutimer [lindex $t 2] }
    }
}

proc ::cs2intel::init {} {
    variable commands
    http::config -useragent [cfg user_agent] -urlencoding utf-8
    setudef flag cs2intel
    foreach c $commands {
        bind pub - "!$c" [list ::cs2intel::pub $c]
        bind msg - $c [list ::cs2intel::msg $c]
        bind msg - "!$c" [list ::cs2intel::msg $c]
    }
    bind dcc n cs2intel ::cs2intel::dcc_status
    kill_timers
    utimer 5 ::cs2intel::poll
    utimer 10 ::cs2intel::heartbeat
    if {[cfg token] eq ""} {
        log "WARNING: cs2intel(token) is empty; the backend will reject requests."
    }
    log "v$::cs2intel::version loaded (network [cfg network], backend [cfg api_url])"
}

::cs2intel::init
