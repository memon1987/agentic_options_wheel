# Test Your Continuous Deployment Pipeline

## Step-by-Step Cloud Build Trigger Setup

### 1. Open Cloud Build Triggers
**URL:** https://console.cloud.google.com/cloud-build/triggers?project=gen-lang-client-0607444019

### 2. Create Trigger
- Click **"CREATE TRIGGER"**

### 3. Configure Trigger
```
Name: deploy-options-wheel-strategy
Description: Automatically deploy options wheel strategy on push to main branch
Event: Push to a branch
```

### 4. Connect Repository
- Click **"CONNECT NEW REPOSITORY"**
- Select **"GitHub (Cloud Build GitHub App)"**
- Click **"CONTINUE"**
- **Authorize Google Cloud Build** in the popup
- Select repository: **memon1987/agentic_options_wheel**
- Click **"CONNECT"**

### 5. Final Configuration
```
Repository: memon1987/agentic_options_wheel
Branch: ^main$
Configuration: Cloud Build configuration file (yaml or json)
Cloud Build configuration file location: /cloudbuild.yaml
```

### 6. Create Trigger
- Click **"CREATE"**

## Test the Pipeline

After setting up the trigger, run this command to test:

```bash
git push origin main
```

## Monitor the Build

**Build Console:** https://console.cloud.google.com/cloud-build/builds?project=gen-lang-client-0607444019

### What to Expect:
1. **🧪 Tests Run** - Pytest validation (2-3 minutes)
2. **🏗️ Build Image** - Container build (3-5 minutes)
3. **📤 Push Image** - Upload to registry (1-2 minutes)
4. **🚀 Deploy** - Update Cloud Run service (1-2 minutes)
5. **✅ Health Check** - Verify deployment (30 seconds)

**Total Time:** ~7-13 minutes

### Success Indicators:
- ✅ Green checkmark in build history
- ✅ New revision deployed to Cloud Run
- ✅ Service responds to health checks

### If Build Fails:
- Check build logs in console
- Verify tests pass locally: `pytest tests/ -v`
- Review cloudbuild.yaml syntax

## After Successful Test:
Your options wheel strategy will have **enterprise-grade CI/CD**!

Every future code push will automatically:
- Run tests
- Build container
- Deploy to production
- Verify health

🎉 **Professional algorithmic trading infrastructure complete!**