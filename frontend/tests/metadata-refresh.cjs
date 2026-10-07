// Run against a local Vite server. Mock transport proves UI behavior only.
const { chromium, expect } = require(process.env.PLAYWRIGHT_MODULE);
const fs = require('node:fs');
const crypto = require('node:crypto');
const path = require('node:path');
const assert = require('node:assert/strict');
const corpus = fs.readFileSync(path.join(__dirname, '../../tests/fixtures/prod_works_v1.json'));
assert.equal(crypto.createHash('sha256').update(corpus).digest('hex'), 'd11651d2162ced23e8d919af0bff2d9f316e203234cc854909ba5f444a35ec32');
const movie = JSON.parse(corpus).tables.movies[0];
const texts = {
 'en-US': {title:'Search and refresh metadata',search:'Search',choose:'Select',apply:'Apply changes',back:'Back to candidates',override:'Override manually edited fields',protected:'Manually protected',update:'Update',empty:'Search to select a candidate',searchError:'Metadata search failed',previewError:'Unable to preview metadata changes',applyError:'Metadata update failed',success:'Updated 1 field'},
 'zh-CN': {title:'搜索并刷新元数据',search:'搜索',choose:'选择',apply:'确认应用',back:'返回候选',override:'覆盖人工编辑字段',protected:'人工保护',update:'更新',empty:'搜索后选择一个候选',searchError:'元数据搜索失败',previewError:'无法预览元数据差异',applyError:'元数据更新失败',success:'已更新 1 个字段'},
};
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_PATH,args:['--no-proxy-server']});
 const results=[];let currentPage;
 try {
  for(const lang of ['en-US','zh-CN']) for(const count of [0,1,2]) {
   const context=await browser.newContext();const page=await context.newPage();currentPage=page;const errors=[];page.on('pageerror',e=>errors.push(e.message));
   await page.addInitScript(lang=>localStorage.setItem('rssripple-lang',lang),lang);
   let failure=null;const requests=[];
   const candidate={...movie,identity_source:movie.external_source,selectable:true,match_path:'recorded-title / synthetic search'};
   await page.route('**/api/v1/**',async route=>{
    const request=route.request();const url=new URL(request.url()).pathname;const body=request.method()==='POST'?request.postDataJSON():null;
    requests.push({url,body});
    let data;
    if(url.endsWith('/sources')) data={primary_sources:[{value:'wikipedia',label:'Wikipedia',available:true}],trusted_sites:[{value:'imdb',domains:['imdb.com']}],default_trusted_sites:['imdb']};
    else if(url.endsWith('/search')) data={candidates:[candidate]};
    else if(url.endsWith('/preview')) data={changes:[{field:'description',current:'Curated original',incoming:'Synthetic incoming',protected:!body.override_manual_edits,action:body.override_manual_edits?'update':'skip'}],warnings:[]};
    else if(url.endsWith('/apply')) data={applied:Array.from({length:count},(_,i)=>'synthetic_field_'+i),skipped:[]};
    else throw Error('Unexpected endpoint '+url);
    const payload=url.endsWith('/'+failure?.split(':')[0])?{success:false,data:null,error:failure.endsWith(':message')?{code:'SYNTHETIC',message:'Source failure / 原始错误'}:null}:{success:true,data,error:null};
    await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(payload)});
   });
   await page.goto(process.env.PROBE_URL+'/tests/metadata-refresh.html');
   page.setDefaultTimeout(8000);const t=texts[lang];const modal=page.getByRole('dialog');
   // Ant Design retains a zero-opacity loading icon during its leave animation
   // and inserts spacing in two-character Chinese buttons. Match their names.
   const button=name=>modal.getByRole('button',{name:new RegExp('^(?:loading\\s+)?'+[...name].join('\\s*')+'$')});
   console.log(JSON.stringify({lang,observedTitle:await modal.locator('.ant-modal-title').innerText()}));
   await expect(modal.getByText(t.title,{exact:true})).toBeVisible();
   await expect(modal.getByText(t.empty,{exact:true})).toBeVisible();
   failure='search';await button(t.search).click();await expect(page.getByText(t.searchError,{exact:true})).toBeVisible();
   failure='search:message';await button(t.search).click();await expect(page.getByText('Source failure / 原始错误',{exact:true})).toBeVisible();
   failure=null;await button(t.search).click();await expect(modal.getByText(movie.title_cn || movie.original_title || movie.title_en,{exact:true})).toBeVisible();
   failure='preview';await button(t.choose).click();await expect(page.getByText(t.previewError,{exact:true})).toBeVisible();
   await button(t.back).click();failure=null;await button(t.choose).click();
   await expect(modal.getByText(t.protected,{exact:true})).toBeVisible();
   await modal.getByRole('checkbox',{name:t.override,exact:true}).check();await expect(modal.getByText(t.update,{exact:true})).toBeVisible();
   assert.equal(requests.filter(x=>x.url.endsWith('/preview')).at(0).body.override_manual_edits,false);
   assert.equal(requests.filter(x=>x.url.endsWith('/preview')).at(-1).body.override_manual_edits,true);
   assert.deepEqual(requests.find(x=>x.url.endsWith('/search')).body,{query:'Synthetic query',content_type:'movie',mode:'online',source:'wikipedia',trusted_sites:['imdb']});
   // Switch while open through real i18next; no remount or data translation.
   await page.evaluate(()=>document.querySelector('button').click());
   const other=texts[lang==='en-US'?'zh-CN':'en-US'];await expect(modal.getByText(other.title,{exact:true})).toBeVisible();
   await page.evaluate(()=>document.querySelector('button').click());await expect(modal.getByText(t.title,{exact:true})).toBeVisible();
   failure='apply';await button(t.apply).click();await expect(page.getByText(t.applyError,{exact:true})).toBeVisible();
   failure=null;await button(t.apply).click();await expect(page.getByText(lang==='zh-CN'?`已更新 ${count} 个字段`:`Updated ${count} ${count===1?'field':'fields'}`,{exact:true})).toBeVisible();await expect(page.getByTestId('applied')).toHaveText('true');
   assert.equal(requests.filter(x=>x.url.endsWith('/apply')).at(-1).body.override_manual_edits,true);
   assert.deepEqual(errors,[]);results.push({lang,count,passed:true,requests:requests.length});await context.close();
  }
  console.log(JSON.stringify({recordedMovie:movie.id,transport:'synthetic API responses',results},null,2));
 } catch(error) {console.error('DOM',await currentPage.locator('body').innerText());console.error('ARIA',await currentPage.locator('body').ariaSnapshot());console.error('BUTTON_HTML',await currentPage.locator('button').evaluateAll(nodes=>nodes.map(n=>n.outerHTML)));throw error;} finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
