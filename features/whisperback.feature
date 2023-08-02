@product
Feature: I can report a bug with WhisperBack
  As a Tails user
  When I experience a bug in Tails
  I want to send a complete bug report to the Tails team

  Scenario: tails-debugging-info succeeds
    Given I have started Tails from DVD without network and logged in
    Then running "sudo /usr/local/sbin/tails-debugging-info" as user "amnesia" succeeds
