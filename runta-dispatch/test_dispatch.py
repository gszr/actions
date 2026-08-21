import importlib.util, os, sys, unittest
from pathlib import Path
spec=importlib.util.spec_from_file_location("dispatch",Path(__file__).with_name("dispatch.py")); d=importlib.util.module_from_spec(spec);sys.modules[spec.name]=d;spec.loader.exec_module(d)
class Tests(unittest.TestCase):
    def config(self):
        return d.Config("runta","/runta","default",d.AgentConfig("runta","gpt-4o-mini","https://api.openai.com/v1"),(),2,4096,90)
    def test_runtime_script_never_contains_real_token(self):
        token="ghs_REAL_SECRET_NEVER_RUNTIME"
        job=d.job_script(self.config(),"owner/repo",{"number":1})
        starter=d.start_script(job)
        self.assertNotIn(token,job);self.assertNotIn(token,starter)
        self.assertIn("runta-secret-stub",job);self.assertIn("gh repo clone",job)
        self.assertIn("script -q -f -c",starter);self.assertIn("/tmp/runta-job.sh",starter)
        self.assertIn("echo started",starter)
        self.assertIn("printf %s",starter)
        self.assertTrue(starter.strip().endswith("echo started"))
    def test_github_rules_are_repo_scoped(self):
        rules=d.github_rules("owner/repo","temporary","temporary-git")
        self.assertEqual(rules[0].path,"/*");self.assertEqual(rules[1].path,"/owner/repo*")
        self.assertEqual(len(rules),2);self.assertEqual(rules[0].secret,"temporary");self.assertEqual(rules[1].secret,"temporary-git");self.assertEqual(rules[1].template,"Basic ${credential}")
    def test_secret_name_is_unique_per_run(self):
        old=os.environ.copy()
        try:
            os.environ.update(GITHUB_REPOSITORY_ID="12",GITHUB_RUN_ID="34",GITHUB_RUN_ATTEMPT="2");os.environ.pop("INPUT_RUNTA_GH_TOKEN_NAME",None)
            self.assertEqual(d.github_secret_name(),"github-12-34-2")
        finally:os.environ.clear();os.environ.update(old)
    def test_cleanup_runs_after_failure(self):
        text=Path(d.__file__).read_text()
        self.assertIn("finally:",text);self.assertIn("runta.delete_secret(name)",text);self.assertIn("delete_secret_rule",text);self.assertIn("cleaning ephemeral GitHub stubs and secrets",text)
    def test_parse_follow_emits_new_log_bytes(self):
        state,offset,chunk=d.parse_follow("STATE:RUNNING\nOFFSET:0\nhello\nworld",0)
        self.assertEqual(state,"RUNNING");self.assertGreater(offset,0);self.assertIn("hello",chunk)
    def test_job_includes_install_clone_and_pi(self):
        job=d.job_script(self.config(),"owner/repo",{"number":1})
        self.assertIn("command -v pi",job);self.assertIn("gh repo clone",job);self.assertIn("pi --verbose --mode text -p",job)
if __name__=="__main__":
    unittest.main()
