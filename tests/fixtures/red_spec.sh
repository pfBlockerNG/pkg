# shellcheck shell=sh
Describe 'red canary'
  It 'fails by construction'
    When call true
    The status should equal 1
  End
End
