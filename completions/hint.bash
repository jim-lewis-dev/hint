# Bash completion for the installed HINT command.
_hint()
{
    local hint_command hint_path
    COMPREPLY=()
    hint_command=${COMP_WORDS[0]}
    if [[ $hint_command == */* ]]; then
        hint_path=$hint_command
    else
        hint_path=$(type -P -- "$hint_command") || return 0
    fi
    [[ -x $hint_path ]] || return 0
    type -P timeout >/dev/null || return 0
    # Bound the whole installed command, including network connect and shutdown.
    mapfile -t COMPREPLY < <(
        timeout --kill-after=0.1s 2s "$hint_path" \
            --complete "$COMP_CWORD" "${COMP_WORDS[@]}" 2>/dev/null
    )
    return 0
}

complete -F _hint hint
