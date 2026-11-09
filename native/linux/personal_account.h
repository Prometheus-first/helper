#ifndef HELPER_PERSONAL_ACCOUNT_H
#define HELPER_PERSONAL_ACCOUNT_H
#include <stddef.h>
#include <string.h>
/* A personal identifier need not start with wxid_. Input may not be terminated. */
static int helper_personal_account(const unsigned char *value,size_t n){
 if(!value||!n||n>127)return 0;
 for(size_t i=0;i<n;i++)if(!((value[i]>='a'&&value[i]<='z')||(value[i]>='A'&&value[i]<='Z')||(value[i]>='0'&&value[i]<='9')||value[i]=='_'||value[i]=='.'||value[i]=='-'))return 0;
 if(n>=3&&!memcmp(value,"gh_",3))return 0;
 const char *reserved[]={"filehelper","weixin","fmessage","newsapp","floatbottle"};
 for(size_t i=0;i<sizeof(reserved)/sizeof(reserved[0]);i++)if(n==strlen(reserved[i])&&!memcmp(value,reserved[i],n))return 0;
 return 1;
}
#endif
